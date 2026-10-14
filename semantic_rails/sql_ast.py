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
from collections.abc import Iterator
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

from . import operators
from .errors import SemanticLayerError
from .sql_preparation import ParameterSlot

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
        "BIGNUMERIC",
        "BIGINT",
        "BOOLEAN",
        "CHAR",
        "DATE",
        "DATETIME",
        "DECIMAL",
        "DOUBLE",
        "FLOAT",
        "FLOAT8",
        "FLOAT64",
        "INT64",
        "INTEGER",
        "NUMBER",
        "NUMERIC",
        "STRING",
        "TEXT",
        "TIMESTAMP",
        "TIMESTAMPTZ",
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
        # Implicit-calendar day series (dialects.SqlDialect.day_series):
        # Snowflake, BigQuery, Trino/Athena and Databricks spellings. Engine-only:
        # the dialects' call_names keep them out of `call` expressions.
        "ARRAY_GENERATE_RANGE",
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
        "EXPLODE",
        "FLATTEN",
        "FLOOR",
        "GENERATE_DATE_ARRAY",
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
        # The current time and a value's physical type, for base time coverage on DuckDB
        # and Postgres (dialects.SqlDialect.now and utc_timestamp). Engine-only:
        # the dialects' call_names keep them out of `call` expressions.
        "NOW",
        "NULLIF",
        # Spark SQL exact percentile aggregate.
        "PERCENTILE",
        "PERCENTILE_CONT",
        "POWER",
        "PG_TYPEOF",
        # ClickHouse exact quantiles (case-sensitive — see canonical map).
        "QUANTILE_EXACT",
        "QUANTILE_EXACT_INCLUSIVE",
        "QUANTILE_CONT",
        "RANK",
        "REPLACE",
        "RIGHT",
        "ROUND",
        "ROW_NUMBER",
        "SEQUENCE",
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
    # Nullable targets preserve NULL on ClickHouse regardless of session settings.
    nullable = re.fullmatch(
        r"Nullable\((Float64|Int64|String|DateTime|DateTime64\(6\)|DECIMAL\(\d+,\d+\))\)", type_name
    )
    if nullable:
        return type_name
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
    # Date or timestamp text compared with a clock or date column, which a warehouse whose
    # comparisons don't coerce text spells with its type (``SqlDialect.temporal_literal``).
    temporal: bool = False


@dataclass(frozen=True)
class SqlParameter:
    """A positional ``?`` that the runtime binds per request; never a value."""

    slot: ParameterSlot


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
        if (
            self.op in {"IS", "IS NOT"}
            and isinstance(self.right, SqlLiteral)
            and not (self.right.value is None or isinstance(self.right.value, bool))
        ):
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"expression: op '{self.op}' accepts only NULL, TRUE or FALSE",
                details={"path": "expression", "op": self.op},
            )
        if self.op in _NULL_LITERAL_GUARDED_OPS and (
            is_null_literal(self.left) or is_null_literal(self.right)
        ):
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                "Comparisons with a null literal must use build_comparison_condition",
            )


@dataclass(frozen=True)
class SqlLike:
    """``fold(expr) [NOT] LIKE fold(pattern)``, both sides folded by the dialect's one lowercase
    function; ``\\`` escapes ``%``, ``_`` and itself in ``pattern``."""

    expr: SqlExpr
    pattern: str
    negated: bool = False


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
class SqlInQuery:
    """``expr IN (SELECT …)``: an uncorrelated subquery returning one column."""

    expr: SqlExpr
    query: SqlSelect


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
    ``name(args [IGNORE NULLS] ORDER BY … [LIMIT n])``. Used by dialects without
    MIN_BY/ARG_MIN — Postgres ``array_agg(x ORDER BY y)``, BigQuery
    ``ARRAY_AGG(x IGNORE NULLS ORDER BY y LIMIT 1)``."""

    call: SqlCall
    order_by: list[SqlOrderTerm] = field(default_factory=list)
    limit: int | None = None
    ignore_nulls: bool = False


@dataclass(frozen=True)
class SqlAggregateFilter:
    """``call FILTER (WHERE where)``: an aggregate that reads only the rows ``where`` keeps."""

    call: SqlExpr
    where: SqlExpr


_INDEX_WRAPPERS = frozenset({"", "OFFSET", "ORDINAL", "SAFE_OFFSET"})


@dataclass(frozen=True)
class SqlIndexAccess:
    """Array subscript: ``expr[index]``, or BigQuery's ``expr[OFFSET(index)]`` /
    ``expr[ORDINAL(index)]`` / ``expr[SAFE_OFFSET(index)]`` (NULL out of bounds)."""

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
    | SqlParameter
    | SqlCall
    | SqlBinary
    | SqlLike
    | SqlIn
    | SqlDatePart
    | SqlInterval
    | SqlExists
    | SqlInQuery
    | SqlIsNull
    | SqlCase
    | SqlCast
    | SqlWindow
    | SqlWithinGroup
    | SqlCallWithOrder
    | SqlAggregateFilter
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
    nulls_last: bool = False

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
    observation_scan: bool = False  # Engine-only coverage/observation scan, filter every read.


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


# What build_comparison_condition does with a null literal on either side: equality becomes
# IS NULL, inequality IS NOT NULL, and ordering or LIKE, always UNKNOWN, refuses. Every other
# op keeps SQL's meaning, including the null-safe IS [NOT] DISTINCT FROM and <=>.
_NULL_TEST_EQUALITY_OPS = frozenset({"=", "IS"})
_NULL_TEST_INEQUALITY_OPS = frozenset({"!=", "<>", "IS NOT"})
_NULL_REFUSED_OPS = frozenset({"<", "<=", ">", ">=", "LIKE", "NOT LIKE"})
# SqlBinary refuses a null literal under these; only the null tests IS / IS NOT may hold one.
_NULL_LITERAL_GUARDED_OPS = (
    _NULL_TEST_EQUALITY_OPS | _NULL_TEST_INEQUALITY_OPS | _NULL_REFUSED_OPS
) - {"IS", "IS NOT"}


def filter_rejects_null(op: Any, value: Any) -> bool:
    """Whether a filter drops the rows whose value is unknown."""
    try:
        word, value = operators.normalize_condition(op, value, path="where", internal=True)
    except SemanticLayerError:
        return False
    return operators.rejects_unknown(word, value)


def build_filter_condition(
    expr: SqlExpr, op: Any, value: Any, *, path: str = "where", temporal: bool = False
) -> SqlExpr:
    """The SQL condition for a ``{field, op, value}`` filter, read from the operator table.

    The single home for the op/value → condition mapping used by every filter lowering
    site. ``not`` keeps rows whose value is unknown: ``(x <> v OR x IS NULL)``, the
    portable form on every dialect. The text words are case-insensitive literal matches, and
    their ``not_`` forms keep unknown rows the same way.
    ``temporal`` says the field is a date or timestamp, so its text values are temporal
    literals.
    """

    def literal(item: Any) -> SqlLiteral:
        return SqlLiteral(item, temporal=temporal and isinstance(item, str))

    word, value = operators.normalize_condition(op, value, path=path, internal=True)
    if word == "missing":
        return SqlIsNull(expr=expr) if value else SqlBinary(expr, "IS NOT", SqlLiteral(None))
    if word in {"is", "not"}:
        values = operators.values_of(word, value)
        if not values:
            # Membership in no values: vacuously false (is) or true (not).
            return SqlLiteral(word == "not")
        literals: list[SqlExpr] = [literal(item) for item in values]
        if word == "is":
            return (
                SqlBinary(expr, "=", literals[0]) if len(literals) == 1 else SqlIn(expr, literals)
            )
        differs = (
            SqlBinary(expr, "<>", literals[0])
            if len(literals) == 1
            else SqlIn(expr, literals, negated=True)
        )
        return SqlBinary(differs, "OR", SqlIsNull(expr=expr))
    if word in operators.RANGE_WORDS:
        return SqlBinary(expr, operators.COMPARISON_SQL[word], literal(value))
    negated = word in operators.NEGATED_WORDS
    like = SqlLike(expr, operators.like_pattern(word, str(value)), negated)
    return SqlBinary(like, "OR", SqlIsNull(expr=expr)) if negated else like


def is_null_literal(expr: object) -> bool:
    return isinstance(expr, SqlLiteral) and expr.value is None


def build_comparison_condition(
    left: SqlExpr, op: str, right: SqlExpr, *, path: str = "expression"
) -> SqlExpr:
    """``left <op> right``, where a null literal on either side follows the op's class. A text
    word reads a dimension's column (``bind``), never an aggregate or a raw expression."""
    word = operators.word_of(op)
    if word in operators.TEXT_WORDS:
        raise SemanticLayerError(
            "INVALID_EXPRESSION_AST",
            f"{path}: '{word}' compares a dimension's column with a text literal",
            details={"path": path, "op": word},
        )
    op_normalized = normalize_sql_binary_operator(op)
    if not (is_null_literal(left) or is_null_literal(right)):
        return SqlBinary(left, op_normalized, right)
    expr = right if is_null_literal(left) else left
    if op_normalized in _NULL_TEST_EQUALITY_OPS:
        return SqlIsNull(expr=expr)
    if op_normalized in _NULL_TEST_INEQUALITY_OPS:
        return SqlBinary(expr, "IS NOT", SqlLiteral(None))
    if op_normalized in _NULL_REFUSED_OPS:
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
                            "To test for no value, compare with = or != against null (a where "
                            "filter says op 'missing'); otherwise pass a non-null scalar value "
                            f"for '{op_normalized}'."
                        ),
                    }
                ],
            },
        )
    return SqlBinary(left, op_normalized, right)


def build_negation(arg: SqlExpr) -> SqlExpr:
    """``NOT arg`` as ``FALSE = arg`` (no unary-NOT node), which has the same truth table.

    The arg goes on the right so the renderer parenthesizes it (``FALSE = (x > y)``); chained
    comparisons are non-associative in Snowflake/Postgres. ``NOT NULL`` is a boolean
    NULL expression, distinct from an authored null literal for comparison lowering.
    """
    if is_null_literal(arg):
        return SqlCast(SqlLiteral(None), "BOOLEAN")
    return SqlBinary(SqlLiteral(False), "=", arg)


def sql_nodes(node: Any) -> Iterator[Any]:
    """Every node of a SQL AST, found generically so no node type can hide a read."""
    if isinstance(node, list | tuple):
        for item in node:
            yield from sql_nodes(item)
    elif is_dataclass(node) and not isinstance(node, type):
        yield node
        for item in fields(node):
            yield from sql_nodes(getattr(node, item.name))
