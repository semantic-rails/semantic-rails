"""Typed SQL AST nodes that the compiler emits and the renderer consumes.

Defines :class:`SqlSelect`, :class:`SqlCte`, :class:`SqlJoin`,
:class:`SqlBinary`, :class:`SqlCall`, :class:`SqlCase`, …, plus the
``SQL_BINARY_OPERATORS`` whitelist that gates which operators the
renderer is allowed to emit. The compiler builds this tree;
:mod:`semantic_rails.renderer` turns it into the rendered SQL string.
Treating SQL as data — not as a string — is how the runtime stays
dialect-portable and inspectable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .errors import SemanticLayerError

SQL_BINARY_OPERATORS = frozenset(
    {
        "!=",
        "*",
        "+",
        "-",
        "/",
        "<",
        "<=",
        # Spark SQL / ClickHouse null-safe equality operator, emitted by
        # those dialects' `null_safe_eq`.
        "<=>",
        "<>",
        "=",
        ">",
        ">=",
        "AND",
        "IS",
        "IS DISTINCT FROM",
        "IS NOT",
        "IS NOT DISTINCT FROM",
        "LIKE",
        "NOT LIKE",
        "OR",
    }
)
SQL_CAST_TYPE_NAMES = frozenset(
    {
        "BIGINT",
        "BOOLEAN",
        "CHAR",
        "DATE",
        "DECIMAL",
        "DOUBLE",
        "FLOAT",
        "INTEGER",
        "NUMBER",
        "NUMERIC",
        "STRING",
        "TEXT",
        "TIMESTAMP",
        "TIMESTAMP_NTZ",
        "VARCHAR",
    }
)
SQL_FUNCTION_NAMES = frozenset(
    {
        "ABS",
        # Trino/Athena percentile aggregate.
        "APPROX_PERCENTILE",
        # BigQuery percentile path (PERCENTILE_CONT is window-only there).
        "APPROX_QUANTILES",
        "ARG_MAX",
        "ARG_MIN",
        # ClickHouse arg-min/max (case-sensitive — see canonical map).
        "ARGMAX",
        "ARGMIN",
        "ARRAY_AGG",
        "AVG",
        "CEIL",
        "CEILING",
        "COALESCE",
        "CONCAT",
        "CONVERT_TIMEZONE",
        "COUNT",
        # Snowflake / BigQuery native conditional-aggregate forms.
        # Emitted by ``dialects.SnowflakeDialect.conditional_aggregate``
        # (and a future BigQuery override) when the pattern
        # ``<AGG>(CASE WHEN cond THEN body END)`` is detected — see
        # ``compiler_parts/bind.py:_maybe_conditional_aggregate``.
        "COUNT_IF",
        "COUNTIF",
        "DATE_ADD",
        "DATEADD",
        "DATEDIFF",
        "DATE_DIFF",
        # Postgres extraction (used to compose date_diff arithmetic).
        "DATE_PART",
        "DATE_TRUNC",
        # BigQuery DATETIME (naive timestamp) family. Bare DATETIME /
        # TIMESTAMP are the two-argument constructors BigQuery uses for
        # timezone conversion (it has no CONVERT_TIMEZONE).
        "DATETIME",
        "DATETIME_ADD",
        "DATETIME_DIFF",
        "DATETIME_TRUNC",
        "EQUAL_NULL",
        "EXP",
        "FLATTEN",
        "FLOOR",
        "GENERATE_SERIES",
        "JSON_EXTRACT",
        "JSON_EXTRACT_STRING",
        "LAG",
        "LEAD",
        "LEFT",
        "LENGTH",
        "LN",
        "LOG",
        "LOWER",
        "MAX",
        "MAX_BY",
        "MEDIAN",
        "MIN",
        "MIN_BY",
        "NULLIF",
        # Spark SQL exact percentile aggregate.
        "PERCENTILE",
        "PERCENTILE_CONT",
        "POWER",
        # ClickHouse exact quantiles (case-sensitive — see canonical map).
        "QUANTILE_EXACT",
        "QUANTILE_EXACT_INCLUSIVE",
        "QUANTILE_CONT",
        "RANK",
        "REPLACE",
        "RIGHT",
        "ROUND",
        "ROW_NUMBER",
        "SQRT",
        "SPLIT",
        "STRING_SPLIT",
        "STR_SPLIT",
        "SUBSTR",
        "SUBSTRING",
        "SUM",
        "SUM_IF",
        # BigQuery TIMESTAMP family / Spark SQL timestamp arithmetic.
        "TIMESTAMP",
        "TIMESTAMP_ADD",
        "TIMESTAMP_DIFF",
        "TIMESTAMP_TRUNC",
        "TIMESTAMPADD",
        "TIMESTAMPDIFF",
        # Timezone conversion, one spelling per dialect family:
        # DuckDB/Postgres `timezone(tz, ts)`, Trino/Athena
        # `at_timezone(with_timezone(ts, src), tgt)`, ClickHouse
        # `toTimeZone(toDateTime(ts, src), tgt)` (case-sensitive — see
        # the canonical map below).
        "AT_TIMEZONE",
        "TIMEZONE",
        "TO_DATE_TIME",
        "TO_TIME_ZONE",
        "TRIM",
        "UNNEST",
        "UPPER",
        "WITH_TIMEZONE",
    }
)
SQL_JOIN_TYPES = frozenset(
    {"CROSS", "FULL", "FULL OUTER", "INNER", "LEFT", "LEFT OUTER", "RIGHT", "RIGHT OUTER"}
)
SQL_SORT_DIRECTIONS = frozenset({"ASC", "DESC"})
SQL_SET_OPERATORS = frozenset({"UNION ALL"})
# ISOWEEK is BigQuery's Monday-start week token — the parity match for
# DuckDB/Postgres/Trino `date_trunc('week', …)` (all ISO-8601 Monday).
SQL_DATE_PARTS = frozenset(
    {"DAY", "WEEK", "ISOWEEK", "MONTH", "QUARTER", "YEAR", "HOUR", "MINUTE", "SECOND"}
)

_FUNCTION_CANONICAL_NAMES = {
    "ARG_MAX": "arg_max",
    "ARG_MIN": "arg_min",
    # ClickHouse built-ins are case-sensitive; keep their canonical case.
    "ARGMAX": "argMax",
    "ARGMIN": "argMin",
    "QUANTILE_EXACT": "quantileExact",
    "QUANTILE_EXACT_INCLUSIVE": "quantileExactInclusive",
    "TO_DATE_TIME": "toDateTime",
    "TO_TIME_ZONE": "toTimeZone",
}
_FUNCTION_CANONICAL_VALUES = frozenset(_FUNCTION_CANONICAL_NAMES.values())
_CAST_TYPE_RE = re.compile(r"^(?P<base>[A-Z_][A-Z0-9_]*)(?:\((?P<args>\d+(?:,\d+)?)\))?$")
_WINDOW_FRAME_RE = re.compile(r"^ROWS BETWEEN (?:UNBOUNDED|\d+) PRECEDING AND CURRENT ROW$")


def _compact_token(value: str) -> str:
    return " ".join(str(value).strip().split())


def _invalid_sql_token(kind: str, value: object, allowed: frozenset[str] | None = None) -> None:
    details: dict[str, Any] = {"token_kind": kind, "token": str(value)}
    if allowed is not None:
        details["allowed"] = sorted(allowed)
    raise SemanticLayerError(
        "INVALID_EXPRESSION_AST", f"Unsafe SQL {kind} token: {value!r}", details=details
    )


def _normalize_keyword_token(kind: str, value: str, allowed: frozenset[str]) -> str:
    normalized = _compact_token(value).upper()
    if normalized not in allowed:
        _invalid_sql_token(kind, value, allowed)
    return normalized


def normalize_sql_binary_operator(op: str) -> str:
    return _normalize_keyword_token("operator", op, SQL_BINARY_OPERATORS)


def normalize_sql_function_name(name: str) -> str:
    # Idempotence: already-canonical names (possibly case-sensitive, e.g.
    # ClickHouse's quantileExactInclusive) pass through unchanged so
    # re-normalizing a constructed node never rejects its own output.
    if name in _FUNCTION_CANONICAL_VALUES:
        return name
    normalized = _normalize_keyword_token("function", name, SQL_FUNCTION_NAMES)
    return _FUNCTION_CANONICAL_NAMES.get(normalized, normalized)


def normalize_sql_join_type(join_type: str) -> str:
    return _normalize_keyword_token("join", join_type, SQL_JOIN_TYPES)


def normalize_sql_sort_direction(direction: str) -> str:
    return _normalize_keyword_token("sort", direction, SQL_SORT_DIRECTIONS)


def normalize_sql_set_operator(operator: str) -> str:
    return _normalize_keyword_token("set operator", operator, SQL_SET_OPERATORS)


def normalize_sql_date_part(part: str) -> str:
    return _normalize_keyword_token("date part", part, SQL_DATE_PARTS)


def normalize_sql_cast_type_name(type_name: str) -> str:
    normalized = _compact_token(type_name).upper()
    normalized = re.sub(r"\s*\(\s*", "(", normalized)
    normalized = re.sub(r"\s*,\s*", ",", normalized)
    normalized = re.sub(r"\s*\)\s*", ")", normalized)
    match = _CAST_TYPE_RE.fullmatch(normalized)
    if match is None or match.group("base") not in SQL_CAST_TYPE_NAMES:
        _invalid_sql_token("cast", type_name, SQL_CAST_TYPE_NAMES)
    return normalized


def normalize_sql_window_frame(frame: str) -> str:
    if not frame:
        return ""
    normalized = _compact_token(frame).upper()
    if _WINDOW_FRAME_RE.fullmatch(normalized) is None:
        _invalid_sql_token("window frame", frame)
    return normalized


@dataclass(frozen=True)
class SqlIdentifier:
    parts: list[str]


@dataclass(frozen=True)
class SqlLiteral:
    value: Any


@dataclass(frozen=True)
class SqlStar:
    qualifier: str = ""


@dataclass(frozen=True)
class SqlCall:
    name: str
    args: list[SqlExpr] = field(default_factory=list)
    distinct: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", normalize_sql_function_name(self.name))


@dataclass(frozen=True)
class SqlBinary:
    left: SqlExpr
    op: str
    right: SqlExpr

    def __post_init__(self) -> None:
        object.__setattr__(self, "op", normalize_sql_binary_operator(self.op))


@dataclass(frozen=True)
class SqlIn:
    expr: SqlExpr
    values: list[SqlExpr]
    negated: bool = False

    def __post_init__(self) -> None:
        if not self.values:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "IN expressions require at least one value"
            )


@dataclass(frozen=True)
class SqlDatePart:
    part: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "part", normalize_sql_date_part(self.part))


@dataclass(frozen=True)
class SqlInterval:
    value: SqlExpr
    unit: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "unit", normalize_sql_date_part(self.unit))


@dataclass(frozen=True)
class SqlExists:
    query: SqlSelect
    negated: bool = False


@dataclass(frozen=True)
class SqlIsNull:
    expr: SqlExpr


@dataclass(frozen=True)
class SqlCaseWhen:
    condition: SqlExpr
    result: SqlExpr


@dataclass(frozen=True)
class SqlCase:
    whens: list[SqlCaseWhen]
    else_expr: SqlExpr | None = None


@dataclass(frozen=True)
class SqlCast:
    expr: SqlExpr
    type_name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "type_name", normalize_sql_cast_type_name(self.type_name))


@dataclass(frozen=True)
class SqlOrderTerm:
    expr: SqlExpr
    direction: str = "ASC"

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", normalize_sql_sort_direction(self.direction))


@dataclass(frozen=True)
class SqlWindow:
    function: SqlExpr
    partition_by: list[SqlExpr] = field(default_factory=list)
    order_by: list[SqlOrderTerm] = field(default_factory=list)
    frame: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", normalize_sql_window_frame(self.frame))


@dataclass(frozen=True)
class SqlWithinGroup:
    function: SqlExpr
    order_by: list[SqlOrderTerm] = field(default_factory=list)


@dataclass(frozen=True)
class SqlCallWithOrder:
    """Aggregate call with function-internal ORDER BY (and optional LIMIT):
    ``name(args ORDER BY … [LIMIT n])``. Used by dialects without
    MIN_BY/ARG_MIN — Postgres ``array_agg(x ORDER BY y)``, BigQuery
    ``ARRAY_AGG(x ORDER BY y LIMIT 1)``."""

    call: SqlCall
    order_by: list[SqlOrderTerm] = field(default_factory=list)
    limit: int | None = None


_INDEX_WRAPPERS = frozenset({"", "OFFSET", "ORDINAL"})


@dataclass(frozen=True)
class SqlIndexAccess:
    """Array subscript: ``expr[index]``, or BigQuery's
    ``expr[OFFSET(index)]`` / ``expr[ORDINAL(index)]``."""

    expr: SqlExpr
    index: SqlExpr
    wrapper: str = ""

    def __post_init__(self) -> None:
        normalized = _compact_token(self.wrapper).upper()
        if normalized not in _INDEX_WRAPPERS:
            _invalid_sql_token("index wrapper", self.wrapper, _INDEX_WRAPPERS)
        object.__setattr__(self, "wrapper", normalized)


@dataclass(frozen=True)
class SqlParameterizedCall:
    """Parameterized aggregate call — ClickHouse combinator syntax:
    ``name(params)(args)`` (e.g. ``quantileExactInclusive(0.5)(x)``)."""

    name: str
    params: list[SqlExpr] = field(default_factory=list)
    args: list[SqlExpr] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", normalize_sql_function_name(self.name))


@dataclass(frozen=True)
class SqlJsonExtract:
    expr: SqlExpr
    path: list[str] = field(default_factory=list)
    as_text: bool = True


@dataclass(frozen=True)
class SqlNamedArg:
    name: str
    value: SqlExpr


SqlExpr = (
    SqlIdentifier
    | SqlLiteral
    | SqlStar
    | SqlCall
    | SqlBinary
    | SqlIn
    | SqlDatePart
    | SqlInterval
    | SqlExists
    | SqlIsNull
    | SqlCase
    | SqlCast
    | SqlWindow
    | SqlWithinGroup
    | SqlCallWithOrder
    | SqlIndexAccess
    | SqlParameterizedCall
    | SqlJsonExtract
)


@dataclass(frozen=True)
class SqlTableRef:
    name: str
    alias: str = ""


@dataclass(frozen=True)
class SqlTableFunction:
    name: str
    args: list[SqlExpr] = field(default_factory=list)
    named_args: list[SqlNamedArg] = field(default_factory=list)
    alias: str = ""
    columns: list[str] = field(default_factory=list)
    lateral: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", normalize_sql_function_name(self.name))


SqlFrom = SqlTableRef | SqlTableFunction


@dataclass(frozen=True)
class SqlField:
    expression: SqlExpr
    alias: str


@dataclass(frozen=True)
class SqlJoin:
    join_type: str
    table: SqlFrom
    on: SqlExpr | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "join_type", normalize_sql_join_type(self.join_type))


@dataclass(frozen=True)
class SqlOrder:
    expression: SqlExpr
    direction: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", normalize_sql_sort_direction(self.direction))


@dataclass(frozen=True)
class SqlSelect:
    select: list[SqlField]
    from_table: SqlFrom | None = None
    joins: list[SqlJoin] = field(default_factory=list)
    where: list[SqlExpr] = field(default_factory=list)
    group_by: list[SqlExpr] = field(default_factory=list)
    having: list[SqlExpr] = field(default_factory=list)
    qualify: list[SqlExpr] = field(default_factory=list)
    order_by: list[SqlOrder] = field(default_factory=list)
    limit: int | None = None
    ctes: list[SqlCte] = field(default_factory=list)
    distinct: bool = False


@dataclass(frozen=True)
class SqlCte:
    name: str
    query: SqlQuery


@dataclass(frozen=True)
class SqlSetQuery:
    operator: str
    queries: list[SqlSelect]

    def __post_init__(self) -> None:
        object.__setattr__(self, "operator", normalize_sql_set_operator(self.operator))


SqlQuery = SqlSelect | SqlSetQuery


_NULL_TEST_EQUALITY_OPS = frozenset({"=", "IS"})
_NULL_TEST_INEQUALITY_OPS = frozenset({"!=", "<>", "IS NOT"})


def build_filter_condition(expr: SqlExpr, op: Any, value: Any, *, path: str = "where") -> SqlExpr:
    """Build the SQL condition for a ``{field, op, value}``-style filter.

    The single home for the op/value → condition mapping used by every
    filter lowering site (query ``where[]``, measure-bound filters,
    metric_filter envelopes, predicate sources). Centralizing it keeps
    NULL semantics consistent everywhere:

    - ``IN`` / ``NOT IN`` take a list of scalars; a bare scalar is
      treated as a one-element list (never character-split); an empty
      list compiles to constant FALSE / TRUE (vacuous membership)
      instead of erroring.
    - ``IS NULL`` / ``IS NOT NULL`` ignore ``value`` entirely.
    - ``value: null`` with ``=`` / ``IS`` lowers to ``expr IS NULL``;
      with ``!=`` / ``<>`` / ``IS NOT`` it lowers to ``expr IS NOT
      NULL``. (Previously only the equality form was mapped — every
      other op rendered ``expr != NULL``, which is always UNKNOWN in
      SQL three-valued logic and silently matched zero rows.)
    - ``value: null`` with ordering / LIKE ops is rejected with a
      structured INVALID_QUERY instead of emitting always-UNKNOWN SQL.
    """
    op_normalized = _compact_token(str(op or "=")).upper()
    if op_normalized in {"IN", "NOT IN"}:
        negated = op_normalized == "NOT IN"
        if value is None:
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"{path}: op '{op_normalized}' requires a list value, got null",
                details={
                    "path": path,
                    "op": op_normalized,
                    "received_value": None,
                    "recovery_hints": [
                        {
                            "code": "USE_LIST_VALUE_OR_NULL_TEST",
                            "message": (
                                f"'{op_normalized}' compares against a list of scalar "
                                "values — pass value: [..]. To test for NULL, use "
                                "op 'IS NULL' / 'IS NOT NULL' instead."
                            ),
                        }
                    ],
                },
            )
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        if not values:
            # `x IN ()` is invalid SQL; an empty membership list is
            # vacuously false (and NOT IN vacuously true). Compile to a
            # constant so the query stays runnable.
            return SqlLiteral(bool(negated))
        return SqlIn(expr=expr, values=[SqlLiteral(item) for item in values], negated=negated)
    if op_normalized == "IS NULL":
        return SqlIsNull(expr=expr)
    if op_normalized == "IS NOT NULL":
        return SqlBinary(expr, "IS NOT", SqlLiteral(None))
    if value is None:
        if op_normalized in _NULL_TEST_EQUALITY_OPS:
            return SqlIsNull(expr=expr)
        if op_normalized in _NULL_TEST_INEQUALITY_OPS:
            return SqlBinary(expr, "IS NOT", SqlLiteral(None))
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"{path}: op '{op_normalized}' cannot compare against null",
            details={
                "path": path,
                "op": op_normalized,
                "received_value": None,
                "why_invalid": (
                    f"'<expr> {op_normalized} NULL' is always UNKNOWN in SQL "
                    "three-valued logic and would silently match zero rows."
                ),
                "recovery_hints": [
                    {
                        "code": "USE_NULL_TEST_OR_SCALAR",
                        "message": (
                            "To test for NULL use op 'IS NULL' / 'IS NOT NULL'; "
                            f"otherwise pass a non-null scalar value for '{op_normalized}'."
                        ),
                    }
                ],
            },
        )
    return SqlBinary(expr, str(op or "="), SqlLiteral(value))
