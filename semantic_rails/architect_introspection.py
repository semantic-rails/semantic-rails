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
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import duckdb

from .architect_scaffold import slug
from .errors import SemanticLayerError
from .sql_identifiers import quote_identifier, quote_relation, relation_parts

MAX_SAMPLE_VALUES = 20
MAX_SAMPLE_CHARS = 200
DEFAULT_PROFILE_ROWS = 1_000_000
MAX_PROFILE_ROWS = 1_000_000
MAX_SUGGESTION_KEY_COLUMNS = 8
MAX_FK_TARGETS_PER_COLUMN = 8

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
_TABLE_PREFIXES = ("fct_", "fact_", "dim_", "stg_", "int_", "raw_", "mart_", "vw_")
_BOOKKEEPING_WORDS = ("updated", "modified", "deleted", "loaded", "synced")
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class DuckDBWarehouse:
    """A read-only DuckDB connection with external access disabled."""

    path: str
    connection: Any

    def execute(self, sql: str, params: list[Any] | None = None) -> Any:
        try:
            return self.connection.execute(sql, params or [])
        except duckdb.PermissionException:
            raise SemanticLayerError(
                "UNSUPPORTED_PLATFORM",
                "introspection cannot read a relation that requires DuckDB external access",
                details={"reason": "external_access_disabled"},
            ) from None

    def rows(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        cursor = self.execute(sql, params)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


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
        connection = duckdb.connect(
            db_path, read_only=True, config={"enable_external_access": "false"}
        )
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
        "estimated_size AS rows_estimate, comment FROM duckdb_tables() "
        "WHERE database_name = current_database() AND NOT internal AND NOT temporary "
        "UNION ALL SELECT schema_name, view_name, 'view', column_count, NULL, comment "
        "FROM duckdb_views() WHERE database_name = current_database() AND NOT internal "
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
        "SELECT 'table' AS kind FROM duckdb_tables() WHERE database_name = current_database() "
        "AND schema_name = ? AND table_name = ? UNION ALL SELECT 'view' FROM duckdb_views() "
        "WHERE database_name = current_database() AND schema_name = ? AND view_name = ?",
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
        "SELECT column_name, data_type, is_nullable, column_default, comment FROM duckdb_columns() "
        "WHERE database_name = current_database() AND schema_name = ? AND table_name = ? "
        "ORDER BY column_index",
        [schema, name],
    )
    constraints = warehouse.rows(
        "SELECT constraint_type, constraint_column_names, referenced_table, "
        "referenced_column_names FROM duckdb_constraints() WHERE database_name = current_database() "
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
    source = quote_relation(f"{schema}.{name}")
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
    (row_count,) = warehouse.execute(f"SELECT count(*) FROM {source}").fetchone()
    sampled = int(row_count) > row_cap
    scan = f"(SELECT * FROM {source} USING SAMPLE {row_cap} ROWS)" if sampled else source
    profiles: list[dict[str, Any]] = []
    for column in wanted:
        quoted = quote_identifier(column)
        data_type = str(available[column]["type"])
        extremes = f", min({quoted}), max({quoted})" if _orderable(data_type) else ", NULL, NULL"
        counted, distinct, nulls, low, high = warehouse.execute(
            f"SELECT count(*), count(DISTINCT {quoted}), count(*) - count({quoted})"
            f"{extremes} FROM {scan} AS t"
        ).fetchone()
        samples = (
            [
                _sample_text(row[0])
                for row in warehouse.execute(
                    f"SELECT DISTINCT {quoted} FROM {scan} AS t WHERE {quoted} IS NOT NULL "
                    f"ORDER BY 1 LIMIT {limit}"
                ).fetchall()
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
                "null_count": int(nulls),
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
    summed = _has_word(name, _SUM_WORDS)
    if average and not summed:
        return "avg", "high", "a per-row price or rate: averaging is safe, summing is not"
    if summed:
        return "sum", "high", "an additive amount or quantity"
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
            roles["times"].append(
                {
                    "column": name,
                    "kind": "date" if _type_head(data_type) == "DATE" else "timestamp",
                    "confidence": "low" if late else "high" if nulls == 0 else "medium",
                    "reason": "bookkeeping timestamp; rarely the time to analyze by"
                    if late
                    else f"{data_type.lower()} column" + (f" ({nulls} nulls)" if nulls else ""),
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
                "class": "event_time",
                **({"description": item["description"]} if item.get("description") else {}),
                **({"default": True} if index == 0 else {}),
            }
            for index, item in enumerate(kept_times)
        },
        "dimensions": {
            item["column"]: {
                "kind": "categorical",
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
                    "expr": {"kind": "column", "column": item["key"]},
                    "default_agg": item["aggregation"],
                    "accumulation": {"kind": "flow"},
                    "value_type": "number",
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


def _has_word(column: str, words: tuple[str, ...]) -> bool:
    parts = set(re.split(r"[^a-z0-9]+", column.lower()))
    return any(word in parts or column.lower().endswith(word) for word in words)


def _suggest_key(
    warehouse: DuckDBWarehouse,
    described: dict[str, Any],
    profile: dict[str, Any],
    entity: str,
    source: str,
) -> dict[str, Any] | None:
    if described["primary_key"]:
        return {
            "columns": described["primary_key"],
            "confidence": "high",
            "reason": "declared PRIMARY KEY",
        }
    rows = int(profile["row_count"])
    sampled = bool(profile["sampled"])
    unique = [
        column["name"]
        for column in profile["columns"]
        if column["rows_profiled"]
        and column["null_count"] == 0
        and column["distinct_count"] == column["rows_profiled"]
        and not _is_container_type(str(column["type"]))
    ]
    preferred = [
        column
        for column in unique
        if column.lower() in {"id", f"{entity}_id", f"{entity}_key", f"{entity}key", f"{entity}id"}
    ]
    if preferred or [column for column in unique if _key_like(column)]:
        column = (preferred or [column for column in unique if _key_like(column)])[0]
        return {
            "columns": [column],
            "confidence": "low" if sampled else "high" if preferred else "medium",
            "reason": (
                f"{column} is unique and never null in "
                + (
                    f"a sample of at most {profile['rows_profiled']} of {rows} rows; "
                    "full-relation uniqueness must be confirmed before applying"
                    if sampled
                    else "every row"
                )
                + (f" and is named for the {entity}" if preferred else "")
            ),
        }
    id_like = [
        column["name"]
        for column in profile["columns"]
        if _key_like(column["name"]) or _has_word(column["name"], _LINE_WORDS)
        if not _is_container_type(str(column["type"]))
    ]
    # A bounded prefix is enough for a tentative composite candidate, but it
    # cannot establish uniqueness outside that prefix.
    id_like = id_like[:MAX_SUGGESTION_KEY_COLUMNS]
    probe = f"(SELECT * FROM {source} LIMIT {MAX_PROFILE_ROWS})" if sampled else source
    for index, first in enumerate(id_like):
        for second in id_like[index + 1 :]:
            checked, present_first, present_second, pairs = warehouse.execute(
                f"SELECT count(*), count({quote_identifier(first)}), count({quote_identifier(second)}), "
                f"count(DISTINCT ({quote_identifier(first)}, {quote_identifier(second)})) FROM {probe}"
            ).fetchone()
            if checked and checked == present_first == present_second == pairs:
                return {
                    "columns": [first, second],
                    "confidence": "low" if sampled else "medium",
                    "reason": (
                        f"({first}, {second}) is unique and non-null in "
                        + (
                            f"the first {checked} of {rows} rows; full-relation uniqueness "
                            "must be confirmed before applying"
                            if sampled
                            else "every row; no single column is"
                        )
                    ),
                }
    if unique:
        return {
            "columns": [unique[0]],
            "confidence": "low",
            "reason": (
                f"{unique[0]} appears unique and non-null "
                + (
                    f"in a sample of at most {profile['rows_profiled']} of {rows} rows; "
                    "full-relation uniqueness must be confirmed before applying"
                    if sampled
                    else "in every row, but is not named like a key"
                )
            ),
        }
    return None


def _key_catalog(warehouse: DuckDBWarehouse) -> dict[str, list[dict[str, Any]]]:
    """Every column of every relation, with whether it is that relation's declared key."""
    keys = {
        (str(row["schema_name"]), str(row["table_name"])): list(row["constraint_column_names"])
        for row in warehouse.rows(
            "SELECT schema_name, table_name, constraint_column_names FROM duckdb_constraints() "
            "WHERE database_name = current_database() AND constraint_type = 'PRIMARY KEY'"
        )
    }
    catalog: dict[str, list[dict[str, Any]]] = {}
    for row in warehouse.rows(
        "SELECT schema_name, table_name, column_name, data_type FROM duckdb_columns() "
        "WHERE database_name = current_database() AND NOT internal "
        "AND schema_name <> '_semantic_rails' ORDER BY schema_name, table_name"
    ):
        schema, table, column = str(row["schema_name"]), str(row["table_name"]), row["column_name"]
        if _dotted(schema, table):
            continue
        catalog.setdefault(str(column).lower(), []).append(
            {
                "relation": _relation_name(schema, table),
                "source": quote_relation(f"{schema}.{table}"),
                "column": str(column),
                "type": str(row["data_type"]),
                "declared_key": keys.get((schema, table)) == [str(column)],
                "has_declared_key": (schema, table) in keys,
            }
        )
    return catalog


def _foreign_keys(
    warehouse: DuckDBWarehouse,
    described: dict[str, Any],
    key: list[str],
    source: str,
    profile: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    links = [
        fk_link(fk["columns"], fk["references"], "high", "declared FOREIGN KEY")
        for fk in described["foreign_keys"]
    ]
    declared = {column for fk in described["foreign_keys"] for column in fk["columns"]}
    # A single-column key identifies this relation; a composite key's parts
    # usually point at other entities (order_id in order lines).
    own_key = set(key) if len(key) == 1 else set()
    candidates = [
        str(column["name"])
        for column in described["columns"]
        if _key_like(str(column["name"]))
        and column["name"] not in own_key
        and column["name"] not in declared
        and not _is_container_type(str(column["type"]))
    ][:MAX_SUGGESTION_KEY_COLUMNS]
    catalog = _key_catalog(warehouse) if candidates else {}
    child_types = {str(column["name"]): str(column["type"]) for column in described["columns"]}
    diagnostics: list[dict[str, str]] = []
    child_sampled = bool(profile["sampled"])
    child_probe = f"(SELECT * FROM {source} LIMIT {MAX_PROFILE_ROWS})" if child_sampled else source
    for column in candidates:
        # Declared keys first, then relations without a declared key (a view,
        # a dbt model without a contract) where the column is unique.
        discovered = sorted(
            (
                target
                for target in catalog.get(column.lower(), [])
                if target["relation"] != described["relation"]
                and (target["declared_key"] or not target["has_declared_key"])
            ),
            key=lambda target: not target["declared_key"],
        )
        targets = []
        for target in discovered:
            if _fk_types_compatible(child_types[column], target["type"]):
                targets.append(target)
            else:
                diagnostics.append(
                    {
                        "column": column,
                        "relation": target["relation"],
                        "reason": f"incompatible key types: {child_types[column]} versus {target['type']}",
                    }
                )
        proposed: list[tuple[dict[str, Any], int | None]] = []
        incomplete = False
        for target in targets[:MAX_FK_TARGETS_PER_COLUMN]:
            try:
                quoted = quote_identifier(target["column"])
                (target_rows,) = warehouse.execute(
                    f"SELECT count(*) FROM (SELECT 1 FROM {target['source']} "
                    f"LIMIT {MAX_PROFILE_ROWS + 1})"
                ).fetchone()
                target_large = int(target_rows) > MAX_PROFILE_ROWS
                if target_large and not target["declared_key"]:
                    # A bounded prefix cannot establish uniqueness for an
                    # undeclared target key.
                    incomplete = True
                    diagnostics.append(
                        {
                            "column": column,
                            "relation": target["relation"],
                            "reason": "undeclared target exceeds the bounded uniqueness probe",
                        }
                    )
                    continue
                if not target["declared_key"]:
                    rows, present, distinct = warehouse.execute(
                        f"SELECT count(*), count({quoted}), count(DISTINCT {quoted}) "
                        f"FROM {target['source']}"
                    ).fetchone()
                    if not rows or present != rows or distinct != rows:
                        continue
                orphans = None
                if not target_large:
                    (orphans,) = warehouse.execute(
                        f"SELECT count(*) FROM {child_probe} AS child "
                        f"WHERE child.{quote_identifier(column)} IS NOT NULL "
                        f"AND NOT EXISTS (SELECT 1 FROM {target['source']} AS parent "
                        f"WHERE parent.{quoted} = child.{quote_identifier(column)})"
                    ).fetchone()
            except SemanticLayerError as exc:
                if exc.code != "UNSUPPORTED_PLATFORM":
                    raise
                incomplete = True
                diagnostics.append(
                    {
                        "column": column,
                        "relation": target["relation"],
                        "reason": "candidate could not be checked safely",
                    }
                )
                continue
            except duckdb.Error:
                incomplete = True
                diagnostics.append(
                    {
                        "column": column,
                        "relation": target["relation"],
                        "reason": "candidate probe is unsupported",
                    }
                )
                continue
            confidence = (
                "low"
                if target_large or orphans or child_sampled
                else "medium"
                if not target["declared_key"]
                else "high"
            )
            evidence = (
                f"; target has more than {MAX_PROFILE_ROWS} rows, so values were not checked"
                if target_large
                else f"; {orphans} rows in the bounded prefix have no match"
                if orphans and child_sampled
                else f"; {orphans} rows here have no match"
                if orphans
                else "; every value in the bounded prefix matches"
                if child_sampled
                else "; every value here matches a row there"
            )
            if (target_large or child_sampled) and not orphans:
                evidence += "; full-relation referential integrity must be confirmed"
            proposed.append(
                (
                    fk_link(
                        [column],
                        {"relation": target["relation"], "columns": [target["column"]]},
                        confidence,
                        f"{target['relation']}.{target['column']} is "
                        + ("its declared key" if target["declared_key"] else "unique there")
                        + evidence,
                    ),
                    orphans,
                )
            )
        # An unmatched child value makes a candidate weaker than one whose
        # observed values all match. Unknown values in a large target remain
        # possible, and cannot establish a unique destination either.
        plausible = [item for item in proposed if not item[1]]
        selected = plausible if plausible else proposed
        ambiguous = len(selected) > 1
        truncated = len(targets) > MAX_FK_TARGETS_PER_COLUMN
        for link, _ in selected:
            if ambiguous or truncated or incomplete:
                link["confidence"] = "low"
                if ambiguous:
                    link["reason"] += "; multiple possible target relations match"
                if truncated:
                    link["reason"] += "; additional target relations were not checked"
                if incomplete:
                    link["reason"] += "; another candidate could not be checked"
                link["reason"] += "; confirm the intended relationship"
            links.append(link)
    return links, diagnostics


def suggest_model(warehouse: DuckDBWarehouse, relation: str) -> dict[str, Any]:
    """Propose a model over ``relation``, each choice with a confidence and a reason.

    Heuristics, in order: declared keys, then uniqueness in the data, then
    names. Nothing is written: ``upsert_model`` holds draft arguments to review.
    """
    described = describe_table(warehouse, relation)
    schema, name = _split_relation(relation)
    source = quote_relation(f"{schema}.{name}")
    profile = profile_columns(warehouse, relation, sample_limit=0)
    entity = entity_name(name)
    key = _suggest_key(warehouse, described, profile, entity, source)
    key_columns = list(key["columns"]) if key else []
    links, foreign_key_diagnostics = _foreign_keys(
        warehouse, described, key_columns, source, profile
    )
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
        **roles,
        "foreign_keys": links,
        **({"foreign_key_diagnostics": foreign_key_diagnostics} if foreign_key_diagnostics else {}),
        **({"unsupported_columns": unsupported_columns} if unsupported_columns else {}),
        "upsert_model": upsert_model_draft(
            entity=entity, relation=described["relation"], key_columns=key_columns, **roles
        ),
    }
