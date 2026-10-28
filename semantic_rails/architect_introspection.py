"""Read-only warehouse introspection for package authoring.

Before a model can be written, an author (or an agent, or the REPL) needs to see
what the warehouse holds: which relations exist, their columns and keys, what
the data looks like, and what a model over a relation could be. This module
answers those questions without ever writing to the warehouse:

- :func:`list_tables` lists tables and views;
- :func:`describe_table` returns columns (types, nullability, defaults) and
  declared primary, unique and foreign keys;
- :func:`profile_columns` counts rows, distinct values and nulls, reports
  min/max and a few sample values (hard-capped), profiling at most one
  million rows;
- :func:`suggest_model` proposes a key, time roles, dimensions, measures (with
  an aggregation) and foreign-key links, each with a confidence and a reason,
  plus draft ``upsert_model`` arguments.

It reads DuckDB files (by path, or a package's ``default_db``) today; the
DuckDB connection is opened read-only with external access disabled, and the
file is never created.
"""

from __future__ import annotations

import contextlib
import itertools
import math
import os
import re
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import duckdb

from .db_parts.common import materialized_duckdb_result
from .db_parts.duckdb_setup import connect_duckdb
from .errors import SemanticLayerError
from .naming import slug
from .sql_identifiers import quote_identifier, relation_parts

MAX_SAMPLE_VALUES = 20
MAX_SAMPLE_CHARS = 200
DEFAULT_PROFILE_ROWS = 1_000_000
MAX_PROFILE_ROWS = 1_000_000
MAX_SUGGESTION_KEY_COLUMNS = 8
# One deadline, checked between statements, covers sampling and the whole key search.
KEY_SEARCH_SECONDS = 60.0

_KEY_SUFFIXES = ("_id", "_key", "_code", "_sk")
_LINE_WORDS = ("line", "line_number", "line_no", "row_number", "seq", "sequence", "position")
_SUM_WORDS = (
    "amount",
    "total",
    "revenue",
    "sales",
    "cost",
    "profit",
    "margin",
    "quantity",
    "qty",
    "units",
    "count",
    "value",
    "net",
    "gross",
    "discount",
    "tax",
    "fee",
)
_AVERAGE_WORDS = ("price", "rate", "ratio", "pct", "percent", "score", "avg", "average")
# A vendor's count of distinct people per row (GitHub's daily "uniques"): adding rows counts a
# person once per day or page, so its suggestion is flagged `additive: false`; the draft never
# applies it.
_DISTINCT_COUNT_WORDS = ("unique", "uniques", "distinct", "visitors", "users", "cloners")
_TABLE_PREFIXES = ("fct_", "fact_", "dim_", "stg_", "int_", "raw_", "mart_", "vw_")
_BOOKKEEPING_WORDS = ("updated", "modified", "deleted", "loaded", "synced")
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class DuckDBWarehouse:
    """A read-only DuckDB connection with external access disabled."""

    path: str
    connection: Any

    def execute(self, sql: str, params: list[Any] | None = None) -> tuple[Any, list[Any]]:
        try:
            return materialized_duckdb_result(self.connection, sql, params)
        except duckdb.PermissionException:
            raise SemanticLayerError(
                "UNSUPPORTED_PLATFORM",
                "introspection cannot read a relation that requires DuckDB external access",
                details={"reason": "external_access_disabled"},
            ) from None

    def rows(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        description, fetched = self.execute(sql, params)
        names = [column[0] for column in description]
        return [dict(zip(names, row, strict=True)) for row in fetched]


@contextlib.contextmanager
def open_duckdb(path: str | os.PathLike[str]) -> Iterator[DuckDBWarehouse]:
    """Open an existing DuckDB file read-only, without external access."""
    db_path = str(path)
    if not os.path.isfile(db_path):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"DuckDB database '{db_path}' does not exist; build it first (for example with "
            "`dbt build`, or runtime validation for a package with a seed)",
            details={"duckdb_path": db_path, "reason": "database_missing"},
        )
    try:
        connection = connect_duckdb(
            db_path, read_only=True, config={"enable_external_access": "false"}
        )
    except SemanticLayerError:
        raise
    except Exception as exc:  # noqa: BLE001 — never surface driver text (paths, PIDs)
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"DuckDB database '{db_path}' could not be opened read-only; another process may "
            "be writing it",
            details={"duckdb_path": db_path, "reason": "database_unreadable"},
        ) from exc
    try:
        yield DuckDBWarehouse(path=db_path, connection=connection)
    finally:
        connection.close()


def package_duckdb_path(project_path: str | os.PathLike[str]) -> str:
    """The DuckDB file a package reads, resolved as the runtime does, without building it."""
    from .runtime import Runtime

    runtime = Runtime.from_path(str(project_path))
    if runtime.warehouse != "duckdb":
        raise SemanticLayerError(
            "UNSUPPORTED_PLATFORM",
            "introspection reads DuckDB packages today; this package's warehouse is "
            f"{runtime.warehouse!r}",
            details={"warehouse": runtime.warehouse},
        )
    return runtime.db_path


def _split_relation(relation: str) -> tuple[str, str]:
    """``schema.table`` or ``table`` (schema ``main``), spelled as package models spell it."""
    parts = relation_parts(relation)
    if parts is None or len(parts) > 2:
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"relation must be a table or view name, optionally schema-qualified (got {relation!r})",
            details={"relation": str(relation)},
        )
    return (parts[0], parts[1]) if len(parts) == 2 else ("main", parts[0])


def _relation_name(schema: str, name: str) -> str:
    return name if schema == "main" else f"{schema}.{name}"


def _dotted(schema: str, name: str) -> bool:
    """The runtime splits relations on dots, so it cannot read these names."""
    return "." in schema or "." in name


def list_tables(warehouse: DuckDBWarehouse, *, schema: str = "") -> list[dict[str, Any]]:
    """Tables and views in the file (``schema`` narrows the list), with column counts."""
    rows = warehouse.rows(
        "SELECT schema_name, table_name AS name, 'table' AS kind, column_count, "
        "estimated_size AS rows_estimate, comment FROM system.main.duckdb_tables() "
        "WHERE database_name = system.main.current_database() AND NOT internal AND NOT temporary "
        "UNION ALL SELECT schema_name, view_name, 'view', column_count, NULL, comment "
        "FROM system.main.duckdb_views() WHERE database_name = system.main.current_database() AND NOT internal "
        "AND NOT temporary ORDER BY 1, 2"
    )
    return [
        {
            "relation": _relation_name(str(row["schema_name"]), str(row["name"])),
            "schema": str(row["schema_name"]),
            "name": str(row["name"]),
            "kind": str(row["kind"]),
            "columns": int(row["column_count"] or 0),
            "rows_estimate": None if row["rows_estimate"] is None else int(row["rows_estimate"]),
            "comment": row["comment"],
        }
        for row in rows
        if not schema or str(row["schema_name"]) == schema
        if str(row["schema_name"]) != "_semantic_rails"
        and not _dotted(str(row["schema_name"]), str(row["name"]))
    ]


def _require_relation(warehouse: DuckDBWarehouse, schema: str, name: str) -> str:
    kinds = warehouse.rows(
        "SELECT 'table' AS kind FROM system.main.duckdb_tables() WHERE database_name = system.main.current_database() "
        "AND schema_name = ? AND table_name = ? UNION ALL SELECT 'view' FROM system.main.duckdb_views() "
        "WHERE database_name = system.main.current_database() AND schema_name = ? AND view_name = ?",
        [schema, name, schema, name],
    )
    if not kinds:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND",
            f"relation {_relation_name(schema, name)!r} does not exist in the warehouse",
            details={"relation": _relation_name(schema, name)},
        )
    return str(kinds[0]["kind"])


def describe_table(warehouse: DuckDBWarehouse, relation: str) -> dict[str, Any]:
    """Columns with types, nullability and defaults, and the declared keys."""
    schema, name = _split_relation(relation)
    kind = _require_relation(warehouse, schema, name)
    columns = warehouse.rows(
        "SELECT column_name, data_type, is_nullable, column_default, comment FROM system.main.duckdb_columns() "
        "WHERE database_name = system.main.current_database() AND schema_name = ? AND table_name = ? "
        "ORDER BY column_index",
        [schema, name],
    )
    constraints = warehouse.rows(
        "SELECT constraint_type, constraint_column_names, referenced_table, "
        "referenced_column_names FROM system.main.duckdb_constraints() WHERE database_name = system.main.current_database() "
        "AND schema_name = ? AND table_name = ? ORDER BY constraint_index",
        [schema, name],
    )
    primary_key: list[str] = []
    unique: list[list[str]] = []
    foreign_keys: list[dict[str, Any]] = []
    not_null: set[str] = set()
    for row in constraints:
        names = [str(column) for column in row["constraint_column_names"] or []]
        kind_name = str(row["constraint_type"])
        if kind_name == "PRIMARY KEY":
            primary_key = names
        elif kind_name == "UNIQUE":
            unique.append(names)
        elif kind_name == "NOT NULL":
            not_null.update(names)
        elif kind_name == "FOREIGN KEY" and not _dotted(schema, str(row["referenced_table"])):
            foreign_keys.append(
                {
                    "columns": names,
                    "references": {
                        # DuckDB permits declared foreign keys only within the
                        # same schema and reports the target table without it.
                        "relation": _relation_name(schema, str(row["referenced_table"] or "")),
                        "columns": [str(column) for column in row["referenced_column_names"] or []],
                    },
                }
            )
    return {
        "relation": _relation_name(schema, name),
        "kind": kind,
        "columns": [
            {
                "name": str(row["column_name"]),
                "type": str(row["data_type"]),
                "nullable": bool(row["is_nullable"]) and str(row["column_name"]) not in not_null,
                "default": row["column_default"],
                "comment": row["comment"],
            }
            for row in columns
        ],
        "primary_key": primary_key,
        "unique": unique,
        "foreign_keys": foreign_keys,
    }


def _sample_text(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text if len(text) <= MAX_SAMPLE_CHARS else text[: MAX_SAMPLE_CHARS - 1] + "…"


def _orderable(data_type: str) -> bool:
    return bool(_scalar_family(data_type)) or _type_head(data_type) == "TIME"


def profile_columns(
    warehouse: DuckDBWarehouse,
    relation: str,
    columns: list[str] | None = None,
    *,
    sample_limit: int = 5,
    max_rows: int = DEFAULT_PROFILE_ROWS,
) -> dict[str, Any]:
    """Row, distinct and null counts, min/max and a few sample values per column.

    At most one million rows are profiled (``max_rows`` can lower that cap; a
    uniform reservoir sample beyond it is reported as ``sampled``), and at
    most 20 distinct sample values of at most 200 characters are returned per
    column. Counting the total rows may inspect the full relation.
    """
    described = describe_table(warehouse, relation)
    schema, name = _split_relation(relation)
    source = _qualified(warehouse, schema, name)
    available = {column["name"]: column for column in described["columns"]}
    wanted = list(columns or available)
    unknown = [column for column in wanted if column not in available]
    if unknown:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND",
            f"columns not in {described['relation']}: {', '.join(unknown)}",
            details={"relation": described["relation"], "unknown_columns": unknown},
        )
    limit = max(0, min(int(sample_limit), MAX_SAMPLE_VALUES))
    row_cap = max(1, min(int(max_rows), MAX_PROFILE_ROWS))
    with _sample(warehouse, source, row_cap) as (scan, row_count):
        return _profile(warehouse, described, wanted, scan, row_count, row_cap, limit)


def _qualified(warehouse: DuckDBWarehouse, schema: str, name: str) -> str:
    """The relation with its catalog, so no temporary table of the same name reads in its place."""
    (catalog,) = warehouse.execute("SELECT system.main.current_database()")[1][0]
    return ".".join(quote_identifier(str(part)) for part in (catalog, schema, name))


class _OutOfTime(Exception):
    """A key search's deadline passed."""


def _in_time(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _OutOfTime


@contextlib.contextmanager
def _sample(
    warehouse: DuckDBWarehouse, source: str, row_cap: int, deadline: float = math.inf
) -> Iterator[tuple[str, int]]:
    """The relation, or above ``row_cap`` rows one repeatable reservoir sample of it in a
    temporary table, which every read of the profile and the key search shares; and its rows."""
    (row_count,) = warehouse.execute(f"SELECT system.main.count(*) FROM {source}")[1][0]
    if int(row_count) <= row_cap:
        yield source, int(row_count)
        return
    sample = quote_identifier(f"semantic_rails_sample_{uuid.uuid4().hex}")  # never a relation's
    _in_time(deadline)
    warehouse.execute(
        f"CREATE TEMP TABLE {sample} AS SELECT * FROM {source} "
        f"USING SAMPLE reservoir({row_cap} ROWS) REPEATABLE (0)"
    )
    try:
        yield sample, int(row_count)
    finally:
        warehouse.execute(f"DROP TABLE IF EXISTS {sample}")  # past the deadline too


def _profile(
    warehouse: DuckDBWarehouse,
    described: dict[str, Any],
    wanted: list[str],
    scan: str,
    row_count: int,
    row_cap: int,
    limit: int,
    deadline: float = math.inf,
) -> dict[str, Any]:
    available = {column["name"]: column for column in described["columns"]}
    sampled = row_count > row_cap
    profiles: list[dict[str, Any]] = []
    for column in wanted:
        _in_time(deadline)
        quoted = quote_identifier(column)
        data_type = str(available[column]["type"])
        extremes = (
            f", system.main.min({quoted}), system.main.max({quoted})"
            if _orderable(data_type)
            else ", NULL, NULL"
        )
        counted, distinct, non_null, low, high = warehouse.execute(
            f"SELECT system.main.count(*), system.main.count(DISTINCT {quoted}), system.main.count({quoted})"
            f"{extremes} FROM {scan} AS t"
        )[1][0]
        samples = (
            [
                _sample_text(row[0])
                for row in warehouse.execute(
                    f"SELECT DISTINCT {quoted} FROM {scan} AS t WHERE {quoted} IS NOT NULL "
                    f"ORDER BY 1 LIMIT {limit}"
                )[1]
            ]
            if limit
            else []
        )
        profiles.append(
            {
                "name": column,
                "type": data_type,
                "rows_profiled": int(counted),
                "distinct_count": int(distinct),
                "null_count": int(counted) - int(non_null),
                "min": _sample_text(low),
                "max": _sample_text(high),
                "samples": samples,
            }
        )
    return {
        "relation": described["relation"],
        "row_count": int(row_count),
        "rows_profiled": min(int(row_count), row_cap),
        "sampled": sampled,
        "columns": profiles,
    }


_CONTAINER_CONSTRUCTORS = frozenset(
    {"ARRAY", "LIST", "STRUCT", "MAP", "ROW", "RECORD", "OBJECT", "VARIANT", "JSON"}
)
_ARRAY_SUFFIX = re.compile(r"(?:\[[0-9]*\]|\bARRAY)\s*$")


def _is_container_type(data_type: str) -> bool:
    upper = str(data_type or "").strip().upper()
    constructor = re.match(r"[A-Z_][A-Z0-9_]*", upper)
    return bool(
        (constructor and constructor.group() in _CONTAINER_CONSTRUCTORS)
        or _ARRAY_SUFFIX.search(upper)
    )


def _type_head(data_type: str) -> str:
    return re.split(r"[\s(<]", str(data_type or "").strip().upper(), maxsplit=1)[0]


def _scalar_family(data_type: str) -> str:
    """Conservative families for role inference and safe FK equality probes."""
    if not str(data_type or "").strip() or _is_container_type(data_type):
        return ""
    head = _type_head(data_type)
    if head == "INTERVAL":
        return ""
    if head in {"DATE", "DATETIME"} or head.startswith("TIMESTAMP"):
        return "time"
    if head in {"BOOLEAN", "BOOL"}:
        return "boolean"
    if head in {"VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "CHARACTER", "TEXT", "STRING", "ENUM"}:
        return "text"
    if head == "UUID":
        return "uuid"
    # INT, INTEGER, INT64, (U)TINY/SMALL/BIG/HUGEINT, UINTEGER, BYTEINT, FLOAT, FLOAT64 ...
    numeric_prefix = re.match(r"U?(?:TINY|SMALL|BIG|HUGE|BYTE)?INT|FLOAT", head)
    if head in {"NUMBER", "NUMERIC", "DECIMAL", "BIGNUMERIC", "DOUBLE", "REAL"} or numeric_prefix:
        return "numeric"
    return ""


def _fk_types_compatible(child_type: str, target_type: str) -> bool:
    if _is_container_type(child_type) or _is_container_type(target_type):
        return False
    child_family, target_family = _scalar_family(child_type), _scalar_family(target_type)
    if child_family and target_family:
        return child_family == target_family
    return bool(child_type and target_type and child_type.upper() == target_type.upper())


def classify_column(name: str, data_type: str) -> str:
    """``time``, ``measure``, ``dimension``, ``key`` or ``unknown`` for one column.

    Scalar type families cover common DuckDB, Postgres, Snowflake and BigQuery
    spellings (``TIMESTAMP_NTZ``, ``character varying``, ``INT64``).
    Container types are left unmodeled; key-like names are never measures.
    """
    if not str(data_type or "").strip():
        return "unknown"
    family = _scalar_family(data_type)
    if not family and _is_container_type(data_type):
        return "unknown"
    if family == "time":
        return "time"
    if _key_like(name):
        return "key"
    if family == "numeric":
        return "measure"
    if family in {"text", "boolean"}:
        return "dimension"
    return "unknown"


def measure_aggregation(name: str) -> tuple[str, str, str]:
    """(aggregation, confidence, reason) for a numeric column, from its name."""
    average = _has_word(name, _AVERAGE_WORDS)
    if _distinct_count_like(name):
        return (
            "sum",
            "low",
            "may be a pre-counted distinct count; adding rows counts a person more than once, "
            "so declare additive: false if it is one",
        )
    summed = _has_word(name, _SUM_WORDS)
    if average and not summed:
        return "avg", "high", "a per-row price or rate: averaging is safe, summing is not"
    if summed:
        return "sum", "high", "an additive amount or quantity"
    if re.search(r"(^|_)(number|rank|position|seq|sequence)$", name.lower()):
        return "sum", "low", "a rank or sequence number; its sum rarely means anything"
    return "sum", "medium", "numeric; sum is the usual default"


def fk_link(
    columns: list[str], references: dict[str, Any], confidence: str, reason: str
) -> dict[str, Any]:
    """A suggested foreign key: ``column`` for one local column, ``columns`` for several."""
    local = {"column": columns[0]} if len(columns) == 1 else {"columns": list(columns)}
    return {**local, "references": references, "confidence": confidence, "reason": reason}


def link_columns(link: dict[str, Any]) -> list[str]:
    return list(link.get("columns") or [link["column"]])


def draft_roles(
    entity: str,
    key_columns: list[str],
    links: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    *,
    rows: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Suggested ``times``, ``dimensions`` and ``measures``, and the columns no role fits.

    Each column has a ``name`` and ``type`` and, where known, a ``null_count``
    (0 once a check proves none), a profiled ``distinct_count`` out of ``rows``,
    declared ``values`` and a ``description``. Key and linked columns are skipped.
    """
    roles: dict[str, Any] = {"times": [], "dimensions": [], "measures": []}
    if len(key_columns) == 1:
        roles["measures"].append(
            {
                "key": f"{entity}_count",
                "kind": "entity_count",
                "aggregation": "count_distinct",
                "confidence": "high",
                "reason": f"counts {entity} rows by their key",
            }
        )
    elif not key_columns:  # row grain: every stored row counts
        count = {"key": f"{entity}_rows", "kind": "row_count", "aggregation": "sum"}
        roles["measures"].append({**count, "confidence": "high", "reason": "counts every row"})
    skip = set(key_columns) | {column for link in links for column in link_columns(link)}
    unmodeled: list[dict[str, Any]] = []
    for column in columns:
        name, data_type = str(column["name"]), str(column["type"])
        if name in skip:
            continue
        role = classify_column(name, data_type)
        nulls = column.get("null_count")
        described = {"description": column["description"]} if column.get("description") else {}
        if role == "time":
            late = _has_word(name, _BOOKKEEPING_WORDS)
            snapshot = _snapshot_clock_like(name)
            roles["times"].append(
                {
                    "column": name,
                    "kind": "date" if _type_head(data_type) == "DATE" else "timestamp",
                    "confidence": "low" if late else "high" if nulls == 0 else "medium",
                    "reason": "bookkeeping timestamp; rarely the time to analyze by"
                    if late
                    else f"{data_type.lower()} column"
                    + (f" ({nulls} nulls)" if nulls else "")
                    + ("; named like a snapshot's as-of time" if snapshot else ""),
                    # A stock on an as-of clock whose key lacks it is refused, not summed.
                    **({"class": "as_of_time"} if snapshot else {}),
                    **described,
                }
            )
        elif role == "measure":
            aggregation, confidence, reason = measure_aggregation(name)
            roles["measures"].append(
                {
                    "key": name,
                    "kind": "aggregate",
                    "aggregation": aggregation,
                    "confidence": confidence,
                    "reason": reason,
                    **({"additive": False} if _distinct_count_like(name) else {}),
                    **described,
                }
            )
        elif role == "dimension":
            values, distinct = list(column.get("values") or []), column.get("distinct_count")
            if values:
                confidence, reason = "high", f"accepted_values test: {len(values)} values"
            elif distinct is None:
                confidence, reason = "medium", f"{data_type.lower()} column"
            elif "BOOL" in data_type.upper() or distinct <= 50:
                confidence, reason = "high", f"{distinct} distinct values"
            elif rows and distinct <= max(1000, rows // 10):
                confidence, reason = "medium", f"{distinct} distinct values"
            else:
                confidence, reason = "low", f"{distinct} distinct values; likely free text"
            roles["dimensions"].append(
                {
                    "column": name,
                    # A boolean column as `categorical` would type its filter values as strings.
                    "kind": "boolean" if _scalar_family(data_type) == "boolean" else "categorical",
                    "confidence": confidence,
                    "reason": reason,
                    **({"values": values} if values else {}),
                    **described,
                }
            )
        elif role == "unknown":
            unmodeled.append(column)
    roles["times"].sort(key=lambda item: _CONFIDENCE_RANK[item["confidence"]])
    return roles, unmodeled


def upsert_model_draft(
    *,
    entity: str,
    relation: str,
    key_columns: list[str],
    times: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
    measures: list[dict[str, Any]],
    description: str = "",
) -> dict[str, Any]:
    """Draft ``upsert_model`` arguments from suggestions, leaving out low-confidence ones."""
    kept_times = [item for item in times if item["confidence"] != "low"]
    draft: dict[str, Any] = {
        "model_id": entity if entity.endswith("s") else f"{entity}s",
        "entity_key": entity,
        "relation": relation,
        "primary_key": key_columns,
        "times": {
            item["column"]: {
                "column": item["column"],
                "kind": item["kind"],
                "class": item.get("class", "event_time"),
                **({"description": item["description"]} if item.get("description") else {}),
                **({"default": True} if index == 0 else {}),
            }
            for index, item in enumerate(kept_times)
        },
        "dimensions": {
            item["column"]: {
                "kind": item.get("kind", "categorical"),
                **({"description": item["description"]} if item.get("description") else {}),
                **({"domain": list(item["values"])} if item.get("values") else {}),
            }
            for item in dimensions
            if item["confidence"] != "low"
        },
        "measures": {
            item["key"]: (
                {
                    "kind": "entity_count",
                    "entity_key": key_columns[0],
                    "accumulation": {"kind": "event"},
                    "value_type": "count",
                }
                if item["kind"] == "entity_count"
                else {
                    "kind": "aggregate",
                    "expr": {"kind": "literal", "value": 1}
                    if item["kind"] == "row_count"
                    else {"kind": "column", "column": item["key"]},
                    "default_agg": item["aggregation"],
                    "accumulation": {"kind": "flow"},
                    "value_type": "count" if item["kind"] == "row_count" else "number",
                    **({"description": item["description"]} if item.get("description") else {}),
                }
            )
            for item in measures
        },
    }
    if description:
        draft["description"] = description
    return draft


def entity_name(table: str) -> str:
    """A singular entity name for a relation: ``fct_order_lines`` -> ``order_line``."""
    name = slug(table, fallback="entity")
    for prefix in _TABLE_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    if name.endswith("ies") and len(name) > 4:
        return name[:-3] + "y"
    if name.endswith(("ses", "xes", "ches", "shes")):
        return name[:-2]
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


def _key_like(column: str) -> bool:
    lowered = column.lower()
    return lowered == "id" or lowered.endswith(_KEY_SUFFIXES)


def _snapshot_clock_like(column: str) -> bool:
    """A time column named like a snapshot's as-of time (``snapshot_date``, ``as_of_date``)."""
    as_of = re.search(r"(?:^|[^a-z0-9])as_of(?:[^a-z0-9]|$)", column.lower())
    return _has_word(column, ("snapshot", "asof")) or as_of is not None


def _distinct_count_like(column: str) -> bool:
    """A count of distinct people per row, and not an average or rate of one."""
    return _has_word(column, _DISTINCT_COUNT_WORDS) and not _has_word(column, _AVERAGE_WORDS)


def _has_word(column: str, words: tuple[str, ...]) -> bool:
    parts = set(re.split(r"[^a-z0-9]+", column.lower()))
    return any(word in parts or column.lower().endswith(word) for word in words)


def _key_candidates(profile: dict[str, Any], entity: str) -> list[list[str]]:
    """Column sets of at most three that could key the profiled rows, most likely first.

    Never-null, non-float columns only: an entity-named column, a key-like column with a line
    number, a key-like column with the clock (two with it), any other column but a measure,
    then two key-like columns. A line number names a designed key; a product at a time, or an
    amount, may repeat. A set whose sample distinct counts multiply to fewer than the sampled
    rows can't be one.
    """
    stats = {str(column["name"]): column for column in profile["columns"]}
    rows = int(profile["rows_profiled"])
    usable = [
        name
        for name, column in stats.items()
        if column["rows_profiled"]
        and column["null_count"] == 0
        and not _is_container_type(str(column["type"]))
        and not re.match(r"FLOAT|DOUBLE|REAL", _type_head(str(column["type"])))
    ]
    named = {"id", f"{entity}_id", f"{entity}_key", f"{entity}key", f"{entity}id"}
    links = [name for name in usable if _key_like(name)][:MAX_SUGGESTION_KEY_COLUMNS]
    roles = {name: classify_column(name, str(stats[name]["type"])) for name in usable}
    clock = [n for n in usable if roles[n] == "time" and not _has_word(n, _BOOKKEEPING_WORDS)][:1]
    pairs = list(itertools.combinations(links, 2))
    ordered = [
        *([name] for name in usable if name.lower() in named),
        *([link, line] for link in links for line in usable if _has_word(line, _LINE_WORDS)),
        *([link, *clock] for link in links if clock),
        *([*pair, *clock] for pair in pairs if clock),
        *([name] for name in usable if roles[name] != "measure"),
        *(list(pair) for pair in pairs),
    ]
    out: list[list[str]] = []
    for columns in ordered:
        distinct = math.prod(int(stats[name]["distinct_count"]) for name in columns)
        if len(set(columns)) == len(columns) and columns not in out and distinct >= rows:
            out.append(columns)
    return out


def _suggest_key(
    warehouse: DuckDBWarehouse,
    described: dict[str, Any],
    profile: dict[str, Any],
    entity: str,
    source: str,
    scan: str,
    deadline: float,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The key suggestion (None for row grain) and how the search ended: ``confirmed``,
    ``none_found`` (with how many sets it ``checked``), ``timed_out`` (with the candidate sets
    left ``unconfirmed``, most likely first, none checked on the whole relation, which no draft
    writes) or ``empty_table``.

    Each candidate (a declared PRIMARY KEY, then declared UNIQUE columns) is refuted on the
    profile's own sample, and a survivor is confirmed never null and unique on the whole
    relation; a rejection moves on. An empty relation keeps only a declared key. ``deadline``
    (``time.monotonic``) is checked before each statement and each ending.
    """
    declared = described["primary_key"]
    if not profile["row_count"] and not declared:
        _in_time(deadline)
        return None, {"status": "empty_table"}  # no rows are no evidence of a key
    candidates = [*([declared] if declared else []), *described["unique"]]
    candidates = [*candidates, *_key_candidates(profile, entity)]
    candidates = [cols for i, cols in enumerate(candidates) if cols not in candidates[:i]]
    index = 0
    try:
        for index, columns in enumerate(candidates):
            keys = ", ".join(quote_identifier(column) for column in columns)
            null = " OR ".join(f"{quote_identifier(column)} IS NULL" for column in columns)
            for relation in [scan, *([source] if profile["sampled"] else [])]:
                _in_time(deadline)
                # Grouped by each column, as joins compare them (a collation's equal values).
                rows, repeated, nulls = warehouse.execute(
                    f"SELECT system.main.count(*), (SELECT system.main.count(*) FROM (SELECT 1 "
                    f"FROM {relation} GROUP BY {keys} HAVING system.main.count(*) > 1) AS r), "
                    f"system.main.count(*) FILTER (WHERE {null}) FROM {relation}"
                )[1][0]
                if nulls or repeated:
                    break  # refuted on the sample, or rejected on the whole relation
            else:
                _in_time(deadline)
                reason = f"({', '.join(columns)}) is never null and unique in all {rows} rows"
                reason = "declared PRIMARY KEY" if columns == declared else reason
                key = {"columns": columns, "confidence": "high", "reason": reason}
                return key, {"status": "confirmed", "checked": index + 1}
    except _OutOfTime:
        return None, {"status": "timed_out", "unconfirmed": candidates[index:]}
    _in_time(deadline)
    return None, {"status": "none_found", "checked": len(candidates)}


def _declared_keys(warehouse: DuckDBWarehouse) -> dict[str, list[dict[str, str]]]:
    """Single-column declared primary keys by lowercased column name, with their types."""
    keys: dict[str, list[dict[str, str]]] = {}
    for row in warehouse.rows(
        "SELECT k.schema_name, k.table_name, c.column_name, c.data_type "
        "FROM system.main.duckdb_constraints() AS k JOIN system.main.duckdb_columns() AS c "
        "ON c.database_name = k.database_name AND c.schema_name = k.schema_name "
        "AND c.table_name = k.table_name "
        "AND c.column_name = system.main.array_extract(k.constraint_column_names, 1) "
        "WHERE k.database_name = system.main.current_database() AND k.constraint_type = 'PRIMARY KEY' "
        "AND system.main.len(k.constraint_column_names) = 1 ORDER BY 1, 2"
    ):
        schema, table, column = str(row["schema_name"]), str(row["table_name"]), row["column_name"]
        if not _dotted(schema, table):
            keys.setdefault(str(column).lower(), []).append(
                {
                    "relation": _relation_name(schema, table),
                    "column": str(column),
                    "type": str(row["data_type"]),
                }
            )
    return keys


def _foreign_keys(
    warehouse: DuckDBWarehouse, described: dict[str, Any], key: list[str]
) -> list[dict[str, Any]]:
    """Declared foreign keys, then key-like columns named like another relation's declared key.

    Inferred links are review evidence from names and declarations only; no
    values are compared.
    """
    links = [
        fk_link(fk["columns"], fk["references"], "high", "declared FOREIGN KEY")
        for fk in described["foreign_keys"]
    ]
    # A single-column key identifies this relation; a composite key's parts
    # usually point at other entities (order_id in order lines).
    skip = {column for fk in described["foreign_keys"] for column in fk["columns"]}
    skip |= set(key) if len(key) == 1 else set()
    candidates = [
        column
        for column in described["columns"]
        if _key_like(str(column["name"])) and column["name"] not in skip
    ]
    declared_keys = _declared_keys(warehouse) if candidates else {}
    for column in candidates:
        targets = [
            target
            for target in declared_keys.get(str(column["name"]).lower(), [])
            if target["relation"] != described["relation"]
            and _fk_types_compatible(str(column["type"]), target["type"])
        ]
        for target in targets:
            links.append(
                fk_link(
                    [str(column["name"])],
                    {"relation": target["relation"], "columns": [target["column"]]},
                    "medium" if len(targets) == 1 else "low",
                    f"{target['relation']}.{target['column']} is its declared key; values were "
                    "not compared"
                    + ("" if len(targets) == 1 else f"; {len(targets)} relations declare this key"),
                )
            )
    return links


def suggest_model(warehouse: DuckDBWarehouse, relation: str) -> dict[str, Any]:
    """Propose a model over ``relation``, each choice with a confidence and a reason.

    The key is a declared one, or one the whole relation confirms (``key_search`` says how the
    search went); without one the draft is a row-grain model that counts its rows. Nothing is
    written: ``upsert_model`` holds draft arguments to review.
    """
    described = describe_table(warehouse, relation)
    schema, name = _split_relation(relation)
    source = _qualified(warehouse, schema, name)
    entity = entity_name(name)
    columns = [column["name"] for column in described["columns"]]
    # Without a profile in time, the roles come from the column types alone.
    profile: dict[str, Any] = {"row_count": None, "sampled": None, "columns": described["columns"]}
    key, search = None, {"status": "timed_out", "unconfirmed": []}
    deadline = time.monotonic() + KEY_SEARCH_SECONDS  # sampling, profiling and the key search
    with (
        contextlib.suppress(_OutOfTime),  # out of time before the search ended: no key
        _sample(warehouse, source, MAX_PROFILE_ROWS, deadline) as (scan, row_count),
    ):
        profile = _profile(
            warehouse, described, columns, scan, row_count, MAX_PROFILE_ROWS, 0, deadline
        )
        key, search = _suggest_key(warehouse, described, profile, entity, source, scan, deadline)
    key_columns = list(key["columns"]) if key else []
    links = _foreign_keys(warehouse, described, key_columns)
    roles, unmodeled = draft_roles(
        entity, key_columns, links, profile["columns"], rows=profile["row_count"]
    )
    unsupported_columns = [
        {
            "column": column["name"],
            "type": column["type"],
            "reason": "container type requires an explicit extraction expression",
        }
        for column in unmodeled
        if _is_container_type(str(column["type"]))
    ]
    return {
        "relation": described["relation"],
        "entity": entity,
        "row_count": profile["row_count"],
        "sampled": profile["sampled"],
        "primary_key": key,
        "key_search": search,
        **roles,
        "foreign_keys": links,
        **({"unsupported_columns": unsupported_columns} if unsupported_columns else {}),
        "upsert_model": upsert_model_draft(
            entity=entity, relation=described["relation"], key_columns=key_columns, **roles
        ),
    }


# Candidate keys a one-per-key search tries, singles first; the first this many by naming order.
MAX_ONE_PER_KEY_CANDIDATES = 12


def _key_base(columns: list[str], fallback: str) -> str:
    """The entity a key names: its first column without a key suffix (``room_id`` -> ``room``)."""
    base = re.sub(r"(?:_id|_key|_code|_sk)$", "", columns[0].lower()) if columns else ""
    return entity_name(base) if base not in {"", "id"} else fallback


def _one_per_key_candidates(profile: dict[str, Any], entity: str) -> list[list[str]]:
    """Column sets that could name one row per key: the entity-named and key-like columns, each
    alone, then pairs of key-like columns. NULLs are allowed (a NULL-key row is no entity, and is
    counted, not refused); floats, containers and columns with fewer than two values are not."""
    usable = [
        str(column["name"])
        for column in profile["columns"]
        if column["rows_profiled"]
        and int(column["distinct_count"]) >= 2
        and not _is_container_type(str(column["type"]))
        and not re.match(r"FLOAT|DOUBLE|REAL", _type_head(str(column["type"])))
    ]
    named = {"id", f"{entity}_id", f"{entity}_key", f"{entity}key", f"{entity}id"}
    links = [name for name in usable if name.lower() in named or _key_like(name)]
    links.sort(key=lambda name: name.lower() not in named)
    singles = [[name] for name in links]
    pairs = [list(pair) for pair in itertools.combinations(links[:MAX_SUGGESTION_KEY_COLUMNS], 2)]
    return [*singles, *pairs][:MAX_ONE_PER_KEY_CANDIDATES]


def _key_evidence(
    warehouse: DuckDBWarehouse,
    relation: str,
    key: list[str],
    others: list[str],
    deadline: float,
) -> dict[str, Any]:
    """What the rows of ``relation`` say about reading it one row per ``key``: rows, rows with a
    NULL key, distinct keys, keys with several rows and the most rows a key has, and each other
    column ``fixed`` (one non-null value in every key) or ``varying`` (the keys holding two or
    more). NULLs never count as a value, as a one-per-key read takes the key's one non-null value."""
    _in_time(deadline)
    keys = ", ".join(quote_identifier(column) for column in key)
    set_ = " AND ".join(f"{quote_identifier(column)} IS NOT NULL" for column in key)
    counted = "".join(
        f", system.main.count(DISTINCT {quote_identifier(column)}) AS d{index}"
        for index, column in enumerate(others)
    )
    several = "".join(
        f", system.main.count(*) FILTER (WHERE d{index} > 1)" for index in range(len(others))
    )
    rows, keyed = warehouse.execute(
        f"SELECT system.main.count(*), system.main.count(*) FILTER (WHERE {set_}) FROM {relation}"
    )[1][0]
    _in_time(deadline)
    distinct, repeated, most, *varying = warehouse.execute(
        f"SELECT system.main.count(*), system.main.count(*) FILTER (WHERE n > 1), "
        f"system.main.max(n){several} FROM (SELECT system.main.count(*) AS n{counted} "
        f"FROM {relation} WHERE {set_} GROUP BY {keys}) AS g"
    )[1][0]
    return {
        "columns": list(key),
        "rows": int(rows),
        "null_key_rows": int(rows) - int(keyed),
        "distinct_keys": int(distinct),
        "keys_with_several_rows": int(repeated),
        "most_rows_per_key": int(most or 0),
        "fixed": [column for column, count in zip(others, varying, strict=True) if not count],
        "varying": [
            {"column": column, "keys": int(count)}
            for column, count in zip(others, varying, strict=True)
            if count
        ],
    }


def _one_per_key_qualifies(evidence: dict[str, Any]) -> bool:
    """A key one-per-key reading adds something to: some column is fixed per key, and the key
    repeats or is missing on some rows (a key every row holds once is a plain key)."""
    return bool(evidence["fixed"]) and bool(
        evidence["keys_with_several_rows"] or evidence["null_key_rows"]
    )


def _coverage(evidence: dict[str, Any]) -> list[str]:
    """The proposal's coverage and what stays row grain, in words."""
    rows, nulls = evidence["rows"], evidence["null_key_rows"]
    notes = [
        f"{rows - nulls} of {rows} rows have a key and read as {evidence['distinct_keys']} "
        f"one-per-key rows; {evidence['keys_with_several_rows']} keys have several rows (at most "
        f"{evidence['most_rows_per_key']})."
    ]
    if nulls:
        notes.append(
            f"{nulls} rows have no key: no lookup reaches them, and the row-grain model over the "
            "same relation still counts them."
        )
    if evidence["varying"]:
        listed = ", ".join(f"{row['column']} ({row['keys']} keys)" for row in evidence["varying"])
        notes.append(
            f"These columns hold two or more values within a key and stay on the row-grain model, "
            f"reached one to many: {listed}."
        )
    return notes


def suggest_one_per_key(
    warehouse: DuckDBWarehouse,
    relation: str,
    key: list[str] | None = None,
    *,
    taken: frozenset[str] = frozenset(),
    link_model: str = "",
    parents: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Propose reading a keyless ``relation`` one row per a key it repeats, from its data.

    ``status`` says what the rows support:
    - ``keyed``: one column is unique and never null on the whole relation, so the table can be
      looked up by it: model it keyed (``suggest_model``). A composite key doesn't stop a
      proposal (nothing can look a row up by one of its parts), and is reported as
      ``primary_key``;
    - ``proposed``: ``proposal`` holds the key, its evidence and coverage, the draft
      ``upsert_model`` arguments (``one_per_key: true``, the columns fixed per key) and, with
      ``link_model`` (the row-grain model over the same relation), the ``upsert_relationship``
      arguments that link its rows to the one-per-key rows;
    - ``none``: no candidate key has a column fixed per key (``reason`` says why);
    - ``timed_out``: the search ran out of time.

    ``candidates`` lists each key tried with its evidence: rows, NULL-key rows, distinct keys,
    keys with several rows, and each other column fixed or varying within a key. ``key`` tries
    only that column set. Above a million rows the candidates are compared on the profile's
    sample and the proposal is confirmed on the whole relation. ``taken`` holds entity and model
    names in use, which the draft's names avoid; ``parents`` maps a lowercased column name to the
    (entity, key column) it names, so a key column fixed per key becomes the draft's parent
    entity. Nothing is written, and nothing here checks data at query time: the author decides.
    """
    described = describe_table(warehouse, relation)
    schema, name = _split_relation(relation)
    source = _qualified(warehouse, schema, name)
    entity = entity_name(name)
    columns = [str(column["name"]) for column in described["columns"]]
    asked = list(key or [])
    unknown = [column for column in asked if column not in columns]
    if unknown:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND",
            f"columns not in {described['relation']}: {', '.join(unknown)}",
            details={"relation": described["relation"], "unknown_columns": unknown},
        )
    answer: dict[str, Any] = {"relation": described["relation"], "row_count": None}
    deadline = time.monotonic() + KEY_SEARCH_SECONDS
    try:
        with _sample(warehouse, source, MAX_PROFILE_ROWS, deadline) as (scan, row_count):
            profile = _profile(
                warehouse, described, columns, scan, row_count, MAX_PROFILE_ROWS, 0, deadline
            )
            answer.update(row_count=profile["row_count"], sampled=profile["sampled"])
            if not row_count:
                return {**answer, "status": "none", "reason": "the relation has no rows"}
            plain = None
            if not asked:
                plain, _ = _suggest_key(
                    warehouse, described, profile, entity, source, scan, deadline
                )
                if plain is not None:
                    answer["primary_key"] = plain
                    if len(plain["columns"]) == 1:
                        return {**answer, "status": "keyed", "reason": _KEYED}
            candidates = [asked] if asked else _one_per_key_candidates(profile, entity)
            readable = {
                str(column["name"])
                for column in profile["columns"]
                if not _is_container_type(str(column["type"]))
            }
            tried = [
                _key_evidence(
                    warehouse,
                    scan,
                    candidate,
                    [
                        column
                        for column in columns
                        if column in readable and column not in candidate
                    ],
                    deadline,
                )
                for candidate in candidates
            ]
            ranked = sorted(
                (row for row in tried if _one_per_key_qualifies(row)),
                key=lambda row: (len(row["columns"]), -len(row["fixed"]), row["null_key_rows"]),
            )
            best = ranked[0] if ranked else None
            if best is not None and profile["sampled"]:
                others = [
                    column
                    for column in columns
                    if column in readable and column not in best["columns"]
                ]
                best = _key_evidence(warehouse, source, best["columns"], others, deadline)
                best = best if _one_per_key_qualifies(best) else None
    except _OutOfTime:
        return {**answer, "status": "timed_out", "reason": "the search ran out of time"}
    answer["candidates"] = tried
    if best is None and plain is not None:
        return {**answer, "status": "keyed", "proposal": None, "reason": _KEYED}
    if best is None:
        if not candidates:
            reason = "no column is named like a key; pass key to try a column set"
        elif any(row["fixed"] for row in tried):
            reason = "each key that fixes a column is held once by every row: a plain key"
        else:
            reason = "every other column holds two or more values within every candidate key"
        return {**answer, "status": "none", "proposal": None, "reason": reason}
    return {
        **answer,
        "status": "proposed",
        "proposal": _one_per_key_proposal(
            described,
            profile,
            best,
            entity,
            taken=taken,
            link_model=link_model,
            parents=parents or {},
        ),
    }


_KEYED = (
    "the relation has a key of its own: model it keyed (suggest_model drafts it), not one per key"
)


def _one_per_key_proposal(
    described: dict[str, Any],
    profile: dict[str, Any],
    evidence: dict[str, Any],
    entity: str,
    *,
    taken: frozenset[str],
    link_model: str,
    parents: Mapping[str, tuple[str, str]],
) -> dict[str, Any]:
    key = list(evidence["columns"])
    base = _key_base(key, entity)
    target = f"{base}_one_per_key" if base in taken or base == link_model else base
    model_id = f"{base}_one_per_key"
    # A key column fixed per key names a parent: the package's entity it names, else a dimension.
    fixed = [column for column in evidence["fixed"] if column not in key]
    named = {column: parents[column.lower()] for column in fixed if column.lower() in parents}
    unnamed = [column for column in fixed if _key_like(column) and column not in named]
    stats = [
        column
        for column in profile["columns"]
        if column["name"] in key or (column["name"] in fixed and column["name"] not in named)
    ]
    roles, _ = draft_roles(target, key, [], stats, rows=profile["row_count"])
    for column in unnamed:
        roles["dimensions"].append(
            {
                "column": column,
                "kind": "categorical",
                "confidence": "high",
                "reason": "fixed per key",
            }
        )
    draft = upsert_model_draft(
        entity=target,
        relation=described["relation"],
        key_columns=key,
        description=f"One row per {', '.join(key)} of {described['relation']}: the columns "
        "fixed per key.",
        **roles,
    )
    draft.update(model_id=model_id, one_per_key=True)
    if named:
        draft["entities"] = {
            parent: {} if column == parent_key else {"expr": column}
            for column, (parent, parent_key) in named.items()
        }
    coverage = _coverage(evidence)
    if unnamed:
        coverage.append(
            f"{', '.join(unnamed)} {'is' if len(unnamed) == 1 else 'are'} fixed per key and "
            "named like another table's key: relate it (upsert_relationship) once that table is "
            "modelled."
        )
    proposal: dict[str, Any] = {
        "key": key,
        "evidence": evidence,
        "coverage": coverage,
        "upsert_model": draft,
    }
    if link_model:
        proposal["link"] = {
            "upsert_relationship": {"from_entity": link_model, "to_entity": target, "columns": key}
        }
    return proposal


# A link is proposed when at least this share of the column's distinct values are target keys.
DEFAULT_LINK_COVERAGE = 0.95
MAX_MISSING_SAMPLES = 5
# Key names every table may use for its own key: they say nothing about which table a column
# refers to.
_GENERIC_KEYS = frozenset({"id", "key", "code", "sk", "pk", "uuid"})


def _names_link(column: str, key: str, entity: str = "") -> bool:
    """Whether ``column`` is named like a reference to ``key`` (of ``entity``): the same name, one
    name ending in the other (``office_room_id`` and ``room_id``; ``building_key`` and
    ``fac_building_key``), or the entity's own name with a key suffix. A generic key (``id``)
    is named only that last way (``customer_id`` for ``customers.id``): every table's ``id`` and
    every ``*_id`` column would otherwise pair with it."""
    column, key = column.lower(), key.lower()
    named = {f"{entity}{suffix}" for suffix in _KEY_SUFFIXES} if entity else set()
    if key in _GENERIC_KEYS:
        return column in named
    return (
        column == key or column.endswith(f"_{key}") or key.endswith(f"_{column}") or column in named
    )


def _link_evidence(
    warehouse: DuckDBWarehouse, source: str, column: str, target: str, key: str, deadline: float
) -> dict[str, Any]:
    """How the values of ``column`` in ``source`` meet ``key`` in ``target``: rows, rows with a
    value, distinct values, how many of them (and of the rows) are ``target`` keys, a few that
    aren't, and whether ``key`` names one ``target`` row (never null, never repeated)."""
    _in_time(deadline)
    col, ref = quote_identifier(column), quote_identifier(key)
    found = f"{col} IN (SELECT {ref} FROM {target} WHERE {ref} IS NOT NULL)"
    rows, valued, distinct, rows_found = warehouse.execute(
        f"SELECT system.main.count(*), system.main.count({col}), "
        f"system.main.count(DISTINCT {col}), system.main.count(*) FILTER (WHERE {found}) "
        f"FROM {source}"
    )[1][0]
    _in_time(deadline)
    (distinct_found,) = warehouse.execute(
        f"SELECT system.main.count(*) FROM (SELECT DISTINCT {col} FROM {source} "
        f"WHERE {col} IS NOT NULL) AS v WHERE {found}"
    )[1][0]
    _in_time(deadline)
    target_rows, target_keys, target_valued = warehouse.execute(
        f"SELECT system.main.count(*), system.main.count(DISTINCT {ref}), "
        f"system.main.count({ref}) FROM {target}"
    )[1][0]
    _in_time(deadline)
    missing = [
        _sample_text(row[0])
        for row in warehouse.execute(
            f"SELECT DISTINCT {col} FROM {source} WHERE {col} IS NOT NULL AND NOT {found} "
            f"ORDER BY 1 LIMIT {MAX_MISSING_SAMPLES}"
        )[1]
    ]
    return {
        "rows": int(rows),
        "rows_with_value": int(valued),
        "distinct_values": int(distinct),
        "distinct_found": int(distinct_found),
        "rows_found": int(rows_found),
        "coverage": round(int(distinct_found) / int(distinct), 4) if distinct else 0.0,
        "row_coverage": round(int(rows_found) / int(valued), 4) if valued else 0.0,
        "missing_samples": missing,
        "target_rows": int(target_rows),
        "target_key_unique": int(target_keys) == int(target_valued) == int(target_rows),
        "column_unique": int(distinct) == int(valued) == int(rows),
    }


def _link_words(column: str, target: dict[str, Any], evidence: dict[str, Any]) -> str:
    """The evidence in words."""
    words = (
        f"{evidence['distinct_found']} of {evidence['distinct_values']} distinct {column} values "
        f"({evidence['coverage']:.1%}) are {target['relation']}.{target['key']} keys, on "
        f"{evidence['rows_found']} of {evidence['rows_with_value']} rows with a value"
    )
    if evidence["rows"] > evidence["rows_with_value"]:
        words += f"; {evidence['rows'] - evidence['rows_with_value']} rows have none"
    if evidence["missing_samples"]:
        words += f"; not found: {', '.join(map(str, evidence['missing_samples']))}"
    return words + "."


def suggest_links(
    warehouse: DuckDBWarehouse,
    relation: str,
    columns: list[str] | None = None,
    *,
    min_coverage: float = DEFAULT_LINK_COVERAGE,
    targets: list[dict[str, Any]] | None = None,
    from_entity: str = "",
    linked: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Propose the relationships from ``relation`` to other tables' keys, from their values.

    A candidate pairs a column of ``relation`` with a key of another table: each of ``targets``
    (``{relation, key, entity}``; by default every declared single-column primary key) that the
    column is named like (``columns`` tries those columns against every target of a compatible
    type, whatever their names). Each pair's ``evidence`` counts the column's values found among
    the target's keys, by distinct value (``coverage``) and by row, with a few values not found.
    A pair whose target key names one row, and whose coverage reaches ``min_coverage`` (the
    stated ``threshold``), is in ``proposals``; one under it is in ``below_threshold`` with the
    same evidence and drafts, for the author to decide (a row whose value isn't found keeps its
    rows, and reads NULL through the link). Each carries draft ``upsert_relationship`` arguments
    when ``from_entity`` (the package's entity or row-grain model over ``relation``) and the
    target's entity are known: ``one_to_one`` when the column is unique and never null too,
    else ``many_to_one``. Targets already linked (``linked``) are skipped, and so is the
    table's own key unless ``columns`` names it. Nothing is written,
    and nothing checks data at query time.
    """
    if not 0 < min_coverage <= 1:
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"min_coverage is a share of distinct values, over 0 and at most 1 (got {min_coverage})",
            details={"min_coverage": min_coverage},
        )
    described = describe_table(warehouse, relation)
    schema, name = _split_relation(relation)
    source = _qualified(warehouse, schema, name)
    types = {str(column["name"]): str(column["type"]) for column in described["columns"]}
    unknown = [column for column in columns or [] if column not in types]
    if unknown:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND",
            f"columns not in {described['relation']}: {', '.join(unknown)}",
            details={"relation": described["relation"], "unknown_columns": unknown},
        )
    if targets is None:
        targets = [
            {"relation": row["relation"], "key": row["column"], "entity": "", "type": row["type"]}
            for rows in _declared_keys(warehouse).values()
            for row in rows
        ]
    answer: dict[str, Any] = {
        "relation": described["relation"],
        "threshold": {"coverage": min_coverage},
        "proposals": [],
        "below_threshold": [],
        "not_proposed": [],
    }
    # The table's own key refers to its own rows, never to another table's.
    own_keys = {
        str(row["column"]).lower()
        for rows in _declared_keys(warehouse).values()
        for row in rows
        if _split_relation(str(row["relation"])) == (schema, name)
    } | {
        str(target["key"]).lower()
        for target in targets
        if _split_relation(str(target["relation"])) == (schema, name)
    }
    described_targets: dict[str, dict[str, str]] = {}
    deadline = time.monotonic() + KEY_SEARCH_SECONDS
    try:
        for target in targets:
            if _split_relation(str(target["relation"])) == (schema, name):
                continue
            if target.get("entity") and target["entity"] in linked:
                continue
            target_types = described_targets.get(str(target["relation"]))
            if target_types is None:
                target_types = {
                    str(column["name"]): str(column["type"])
                    for column in describe_table(warehouse, str(target["relation"]))["columns"]
                }
                described_targets[str(target["relation"])] = target_types
            key = str(target["key"])
            if key not in target_types:
                continue
            entity = str(
                target.get("entity") or entity_name(_split_relation(target["relation"])[1])
            )
            for column in columns or list(types):
                if not _fk_types_compatible(types[column], target_types[key]):
                    continue
                if not columns and (
                    column.lower() in own_keys or not _names_link(column, key, entity)
                ):
                    continue
                target_schema, target_name = _split_relation(str(target["relation"]))
                evidence = _link_evidence(
                    warehouse,
                    source,
                    column,
                    _qualified(warehouse, target_schema, target_name),
                    key,
                    deadline,
                )
                _place_link(answer, column, target, evidence, min_coverage, from_entity)
    except _OutOfTime:
        answer["timed_out"] = True
    for bucket in ("proposals", "below_threshold", "not_proposed"):
        answer[bucket].sort(key=lambda row: (-row["evidence"]["coverage"], row["column"]))
    return answer


def _place_link(
    answer: dict[str, Any],
    column: str,
    target: dict[str, Any],
    evidence: dict[str, Any],
    min_coverage: float,
    from_entity: str,
) -> None:
    row: dict[str, Any] = {
        "column": column,
        "target": {key: target[key] for key in ("relation", "key", "entity") if target.get(key)},
        "evidence": evidence,
    }
    if not evidence["distinct_found"]:
        return  # a column named like a key whose values are none of its keys: no candidate
    if not evidence["target_key_unique"]:
        row["reason"] = (
            f"{target['relation']}.{target['key']} repeats or is missing on some rows, so it "
            "names no one row: no lookup can read it (suggest_one_per_key says whether that "
            "table can be read one row per key)"
        )
        answer["not_proposed"].append(row)
        return
    cardinality = "one_to_one" if evidence["column_unique"] else "many_to_one"
    row["cardinality"] = cardinality
    row["reason"] = _link_words(column, target, evidence)
    if from_entity and target.get("entity"):
        row["upsert_relationship"] = {
            "from_entity": from_entity,
            "to_entity": target["entity"],
            "columns": [column],
            "cardinality": cardinality,
        }
    answer["proposals" if evidence["coverage"] >= min_coverage else "below_threshold"].append(row)
