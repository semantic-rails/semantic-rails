"""Canonical JaffleShop fixture for cross-warehouse conformance tests.

ONE canonical source: the repo's ``data/jaffle_csv`` CSVs transformed by
``data/seed_jaffle.sql`` — exactly the pipeline the built-in jaffle_shop
package uses on DuckDB. We materialize that pipeline once into a DuckDB
file, export every final ``jaffle_*`` table to parquet, and record a
manifest (columns, logical types, row counts) plus a content
fingerprint. Each warehouse loader loads the IDENTICAL parquet rows, so
cross-dialect results are directly comparable, and the DuckDB file
itself doubles as the reference target's database.

The fixture is content-addressed under ``data/.fixture_cache/`` (git-
ignored): it rebuilds only when the seed CSVs or seed SQL change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CSV_DIR = REPO_ROOT / "data" / "jaffle_csv"
SEED_SQL = REPO_ROOT / "data" / "seed_jaffle.sql"
CACHE_ROOT = REPO_ROOT / "data" / ".fixture_cache"

# Bump to force a fixture rebuild + warehouse reload when the EXPORT
# format changes (not just the seed inputs). v2: HUGEINT columns are
# cast to BIGINT at export — parquet has no int128, so DuckDB wrote
# them as DOUBLE, which broke loaders with explicit integer schemas
# (BigQuery load jobs reject DOUBLE parquet for INT64 columns).
FIXTURE_FORMAT_VERSION = 2

# DuckDB type name (prefix) -> portable logical type. Loaders map the
# logical type to their warehouse's DDL type; see loaders/__init__.py.
_LOGICAL_TYPES: tuple[tuple[str, str], ...] = (
    ("DECIMAL", "decimal"),
    ("TINYINT", "integer"),
    ("SMALLINT", "integer"),
    ("INTEGER", "integer"),
    ("BIGINT", "integer"),
    ("HUGEINT", "integer"),
    ("DOUBLE", "float"),
    ("FLOAT", "float"),
    ("REAL", "float"),
    ("VARCHAR", "string"),
    ("DATE", "date"),
    ("TIMESTAMP", "timestamp"),
    ("BOOLEAN", "boolean"),
)


@dataclass(frozen=True)
class FixtureColumn:
    name: str
    duckdb_type: str
    logical_type: str


@dataclass(frozen=True)
class FixtureTable:
    name: str
    columns: tuple[FixtureColumn, ...]
    row_count: int

    def parquet_name(self) -> str:
        return f"{self.name}.parquet"


@dataclass(frozen=True)
class JaffleFixture:
    path: Path  # directory holding parquet files + manifest + reference duckdb
    fingerprint: str
    tables: tuple[FixtureTable, ...]

    @property
    def reference_db(self) -> Path:
        return self.path / "jaffle_reference.duckdb"

    def table(self, name: str) -> FixtureTable:
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(name)

    def parquet_path(self, table: str) -> Path:
        return self.path / f"{table}.parquet"


def _logical_type(duckdb_type: str) -> str:
    upper = duckdb_type.upper()
    for prefix, logical in _LOGICAL_TYPES:
        if upper.startswith(prefix):
            return logical
    raise ValueError(f"No logical-type mapping for DuckDB type '{duckdb_type}'")


def seed_fingerprint() -> str:
    """Content hash of every seed input — CSVs + seed SQL + export format."""
    digest = hashlib.sha256()
    digest.update(f"format:v{FIXTURE_FORMAT_VERSION}".encode())
    digest.update(SEED_SQL.read_bytes())
    for csv in sorted(CSV_DIR.glob("*.csv")):
        digest.update(csv.name.encode())
        digest.update(csv.read_bytes())
    return digest.hexdigest()[:16]


def build_jaffle_fixture() -> JaffleFixture:
    """Build (or reuse) the canonical fixture; idempotent by content hash."""
    import duckdb

    from semantic_rails.db import load_csv_dir_to_duckdb

    fingerprint = seed_fingerprint()
    target_dir = CACHE_ROOT / fingerprint
    manifest_path = target_dir / "manifest.json"
    if manifest_path.exists():
        return _load_manifest(target_dir, fingerprint)

    target_dir.mkdir(parents=True, exist_ok=True)
    db_path = target_dir / "jaffle_reference.duckdb"
    load_csv_dir_to_duckdb(str(db_path), str(CSV_DIR), str(SEED_SQL))

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        table_names = [
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name LIKE 'jaffle_%' ORDER BY table_name"
            ).fetchall()
        ]
        tables: list[FixtureTable] = []
        for name in table_names:
            described = con.execute(f"DESCRIBE {name}").fetchall()
            # parquet has no int128: DuckDB writes HUGEINT as DOUBLE, which
            # breaks loaders that declare integer columns. The fixture's
            # HUGEINT values are sums of cents — cast to BIGINT at export
            # so every warehouse sees a true integer column.
            columns = tuple(
                FixtureColumn(
                    name=col[0],
                    duckdb_type="BIGINT" if col[1].upper().startswith("HUGEINT") else col[1],
                    logical_type=_logical_type(col[1]),
                )
                for col in described
            )
            select_list = ", ".join(
                f'CAST("{col[0]}" AS BIGINT) AS "{col[0]}"'
                if col[1].upper().startswith("HUGEINT")
                else f'"{col[0]}"'
                for col in described
            )
            order_by = ", ".join(f'"{col.name}"' for col in columns)
            out = str(target_dir / f"{name}.parquet").replace("'", "''")
            con.execute(
                f"COPY (SELECT {select_list} FROM {name} ORDER BY {order_by}) TO '{out}' (FORMAT PARQUET)"
            )
            row_count = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            tables.append(FixtureTable(name=name, columns=columns, row_count=row_count))
    finally:
        con.close()

    manifest = {
        "fingerprint": fingerprint,
        "tables": [
            {
                "name": t.name,
                "row_count": t.row_count,
                "columns": [
                    {"name": c.name, "duckdb_type": c.duckdb_type, "logical_type": c.logical_type}
                    for c in t.columns
                ],
            }
            for t in tables
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return JaffleFixture(path=target_dir, fingerprint=fingerprint, tables=tuple(tables))


def _load_manifest(target_dir: Path, fingerprint: str) -> JaffleFixture:
    manifest = json.loads((target_dir / "manifest.json").read_text())
    tables = tuple(
        FixtureTable(
            name=entry["name"],
            row_count=entry["row_count"],
            columns=tuple(
                FixtureColumn(
                    name=col["name"],
                    duckdb_type=col["duckdb_type"],
                    logical_type=col["logical_type"],
                )
                for col in entry["columns"]
            ),
        )
        for entry in manifest["tables"]
    )
    return JaffleFixture(path=target_dir, fingerprint=fingerprint, tables=tables)
