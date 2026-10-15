"""SQL dialect adapters — DuckDB, Snowflake, and warehouse capabilities.

Defines :class:`SqlDialect` and concrete subclasses for the supported
warehouses, plus the :func:`dialect_for_warehouse` factory the renderer
and compiler call. Also owns the connection-option schema used by
``parse-config`` to validate warehouse blocks (``supported_warehouses``,
``connection_option_errors``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from .errors import SemanticLayerError
from .expressions import (
    CONVERSION_WINDOW_UNITS,
    PORTABLE_CALL_NAMES,
    call_cast_type,
    call_date_diff_unit,
    validate_call_name,
)
from .sql_ast import (
    SqlAggregateFilter,
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCast,
    SqlDatePart,
    SqlField,
    SqlIdentifier,
    SqlInterval,
    SqlIsNull,
    SqlJoin,
    SqlJsonExtract,
    SqlLiteral,
    SqlNamedArg,
    SqlOrderTerm,
    SqlSelect,
    SqlTableFunction,
    SqlTableRef,
    SqlWithinGroup,
)
from .sql_preparation import (
    PreparedQuery,
    bigquery_compat_sql,
    float_nullif_divisions,
    postgres_compat_sql,
    rewrite_double_quoted_identifiers,
)


def hash_joinable_null_safe_eq(left: Any, right: Any, *, text_cast_type: str) -> Any:
    """Null-safe equality paired with a planner-friendly plain equality.

    Some planners (PostgreSQL, BigQuery) reject or refuse to hash/merge
    FULL OUTER JOINs whose condition is only ``IS NOT DISTINCT FROM`` —
    and the compiler combines measure leaves with FULL OUTER JOINs on
    null-safe key equality. Emit a conjunction instead:

    - a hash-joinable plain equality over text-normalized,
      NULL-coalesced operands (the planner's join clause), AND
    - the exact ``IS NOT DISTINCT FROM`` predicate (the filter).

    The first clause is implied by the second (equal values of one type
    render identical text; two NULLs both coalesce to the sentinel), so
    sentinel collisions can only produce candidate pairs that the exact
    predicate filters out — semantics are unchanged while the planner
    gets its joinable clause. ``text_cast_type`` is the dialect's text
    type (``TEXT`` on Postgres, ``STRING`` on BigQuery).
    """

    def _text_of(operand: Any) -> Any:
        return SqlCall("COALESCE", [SqlCast(operand, text_cast_type), SqlLiteral("__sr_null__")])

    return SqlBinary(
        SqlBinary(_text_of(left), "=", _text_of(right)),
        "AND",
        SqlBinary(left, "IS NOT DISTINCT FROM", right),
    )


def backslash_escaped_string_literal(value: str) -> str:
    """Quote ``value`` for dialects where ``\\`` escapes inside string literals.

    Snowflake and ClickHouse treat a backslash as introducing an escape
    sequence inside a single-quoted literal. Doubling only the quote is
    therefore not enough: a value ending in a backslash escapes the
    *closing* quote, so everything the caller supplied after it is parsed
    as SQL rather than as data.

    Backslashes are doubled before quotes so the doubling cannot be
    applied to a quote-escape this function introduced itself.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def backslash_quoted_string_literal(value: str) -> str:
    """Quote ``value`` where only a backslash escapes a quote: BigQuery reads ``''`` inside
    a literal as two literals (an error) and Spark as two concatenated ones (the quote is
    lost), so the quote is ``\\'`` and a backslash ``\\\\``."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


# An ISO date or naive timestamp, the text a TIMESTAMP literal reads.
_ISO_TEMPORAL = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?)?")


def _value_and_key_known(expr: Any, order_expr: Any) -> Any:
    """The rows a first or last value reads: its value and its order key both known. DuckDB's
    ``arg_min``/``arg_max``, the reference, skip a row where either is NULL."""
    known = SqlBinary(expr, "IS NOT", SqlLiteral(None))
    return SqlBinary(known, "AND", SqlBinary(order_expr, "IS NOT", SqlLiteral(None)))


def _null_where_null(expr: Any, value: Any) -> Any:
    """``value``, NULL where ``expr`` is NULL."""
    return SqlCase([SqlCaseWhen(SqlIsNull(expr), SqlLiteral(None))], else_expr=value)


def _day_rows(day: Any, source: str, series: SqlTableFunction | None = None) -> SqlSelect:
    """``SELECT <day> AS date_day FROM <source> [CROSS JOIN <series>]``."""
    return SqlSelect(
        select=[SqlField(day, "date_day")],
        from_table=SqlTableRef(name=source),
        joins=[SqlJoin("CROSS", table=series)] if series is not None else [],
    )


@dataclass(frozen=True)
class SqlDialect:
    name: str
    # Whether LIKE needs an explicit ``ESCAPE '\\'`` (BigQuery and ClickHouse escape with a
    # backslash by default and have no ESCAPE clause).
    like_escape_clause = True
    # Whether names the SQL leaves unquoted are case-sensitive (ClickHouse); elsewhere a warehouse
    # reads them in any ASCII case.
    identifiers_case_sensitive = False
    # How a text word folds both its column and its text ("{}" is the operand), so the two always
    # fold alike: one Unicode-aware lowercase function on every warehouse.
    case_fold = "LOWER({})"
    # The type a NULL boolean casts to.
    null_boolean_type = "BOOLEAN"
    # Whether the adapter runs each query in the zone of its time role (the runtime's
    # ``_time_zone``), so zone-aware values bucket and compare in that zone.
    session_zone = False
    # Whether a filter across a one-to-many hop may run as a correlated EXISTS. Without it, a
    # de-duplicated parent leaf (one row per key before aggregating) answers the filter.
    correlated_exists = True
    # Whether loaded coverage runs here (:meth:`utc_timestamp`), so a named window under a grain
    # answers every period it holds: 0 inside the data, NULL after its edge.
    reads_coverage = False
    # Whether an EXISTS subquery may sit in a select expression, not only in WHERE.
    exists_in_select = False
    # The aggregates that read a true/false column's lowest and highest value in a group (a
    # one-per-key model's single value): MIN and MAX where they take a boolean.
    boolean_bounds: ClassVar[tuple[str, str]] = ("MIN", "MAX")
    # How a refusal that only this warehouse makes names it.
    label: ClassVar[str] = "This warehouse"
    # The scalar calls a package or query expression may name (``scalar_call``).
    call_names: ClassVar[frozenset[str]] = frozenset()
    # The DATE_DIFF units whose boundary count matches the portable meaning.
    date_diff_units: ClassVar[tuple[str, ...]] = CONVERSION_WINDOW_UNITS
    # Whether a CAST call can enforce DECIMAL(p,s).
    decimal_cast = True

    def scalar_call(self, name: str, args: list[Any], *, distinct: bool = False) -> Any:
        """Central lowering guard for every query, package and relation call."""
        name = validate_call_name(name, self.call_names)
        if distinct:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "Scalar calls do not support distinct"
            )
        if name == "DATE_DIFF":
            unit = call_date_diff_unit(
                args[0].value if args and isinstance(args[0], SqlLiteral) else None,
                arg_count=len(args),
                warehouse=self.name,
                units=self.date_diff_units,
            )
            return self.call_date_diff(unit, args[1], args[2])
        if name != "CAST":
            return SqlCall(name, args)
        value = args[1].value if len(args) == 2 and isinstance(args[1], SqlLiteral) else None
        logical_type = call_cast_type(value, decimal=self.decimal_cast, warehouse=self.name)
        return SqlCast(args[0], self.cast_type_name(logical_type))

    def call_date_diff(self, unit: str, start_expr: Any, end_expr: Any) -> Any:
        """An authored ``DATE_DIFF`` call: boundaries counted on the values' own calendar."""
        return self.date_diff(unit, start_expr, end_expr)

    def cast_type_name(self, logical_type: str) -> str:
        """The warehouse's spelling of a CAST call's logical type (``CALL_CAST_FORMS``)."""
        base = logical_type.split("(")[0]
        return "BIGINT" if base in {"INTEGER", "BIGINT"} else logical_type

    def prepare_query(self, sql: str) -> PreparedQuery:
        """Finalize the executable statement and result-column mapping, once, before
        execution: the warehouse's last syntax and precision rules."""
        return PreparedQuery(sql)

    def quote_string_literal(self, value: str) -> str:
        """Render ``value`` as a single-quoted SQL string literal.

        Standard-conforming default: the single quote is the only
        character with meaning inside a literal, and it is escaped by
        doubling it. This is correct for DuckDB, MotherDuck, DuckLake,
        Postgres (``standard_conforming_strings=on``, the default since
        9.1) and Trino/Athena. Dialects that also honour backslash
        escapes override this with
        :func:`backslash_escaped_string_literal` or
        :func:`backslash_quoted_string_literal` — doubling backslashes
        *here* would corrupt the value on these warehouses instead.
        """
        return "'" + value.replace("'", "''") + "'"

    def temporal_literal(self, value: str) -> str:
        """Date or timestamp text compared with a clock or date column (``SqlLiteral.temporal``);
        the comparison coerces the text."""
        return self.quote_string_literal(value)

    def percentile_cont(self, expr: Any, percentile: float) -> Any:
        return SqlCall("QUANTILE_CONT", [expr, SqlLiteral(percentile)])

    def median(self, expr: Any) -> Any:
        return SqlCall("MEDIAN", [expr])

    def first_value(self, expr: Any, order_expr: Any) -> Any:
        """``expr`` at the least ``order_expr``, over the rows where both are known: a row
        with a NULL value or a NULL key is skipped, as DuckDB's ``arg_min`` skips it."""
        return SqlCall("arg_min", [expr, order_expr])

    def last_value(self, expr: Any, order_expr: Any) -> Any:
        return SqlCall("arg_max", [expr, order_expr])

    def timestamp_type_name(self) -> str:
        return "TIMESTAMP"

    def timestamp_cast(self, expr: Any) -> Any:
        return SqlCast(expr, self.timestamp_type_name())

    def naive_date_clock(self, expr: Any) -> Any:
        """A DATE clock as the input :meth:`convert_timezone` reads as storage-zone midnight.

        DuckDB and Postgres can resolve ``timezone(zone, DATE)`` through the session zone,
        so they cast to a naive timestamp first. Only those executions are tested; every
        other warehouse passes the DATE to its conversion unchanged.
        """
        return expr

    @property
    def has_time_coverage(self) -> bool:
        """Whether every series' empty time bucket reads 0 only inside the base's loaded range.

        That coverage compares stored instants with the request's ``now`` through
        :meth:`utc_timestamp`. DuckDB (and MotherDuck and DuckLake, which run it) and Postgres,
        the warehouses whose execution is tested, read it for every series; the others keep the
        in-window observation test, and read coverage only for a named window
        (:attr:`reads_coverage`).
        """
        return isinstance(self, DuckDbDialect | PostgresDialect)

    def utc_timestamp(self, expr: Any, storage_zone: str) -> Any:
        """UTC wall time for naive storage in its declared zone (``column_timezone``).

        On a warehouse without session-zone queries a package stores naive timestamps and
        declares their zone, so the stored value converts from that zone. Only used where
        :attr:`reads_coverage` holds.
        """
        stored = self.timestamp_cast(expr)
        if storage_zone == "UTC":
            return stored
        return self.convert_timezone(storage_zone, "UTC", stored)

    def date_trunc(self, grain: str, ts_expr: Any) -> Any:
        return SqlCall("DATE_TRUNC", [SqlLiteral(grain), self.timestamp_cast(ts_expr)])

    def convert_timezone(self, source_tz: str, target_tz: str, ts_expr: Any) -> Any:
        """Reinterpret a naive timestamp from ``source_tz`` into ``target_tz``.

        There is no portable spelling for this, so there is no portable
        default. The base used to emit Snowflake's
        ``CONVERT_TIMEZONE(source, target, expr)`` for every warehouse,
        which compiled fine and then failed at execute time on the seven
        that do not have that function — including DuckDB, the reference
        warehouse. Failing to compile is the honest outcome: a dialect
        that supports the rewrite overrides this.
        """
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            (
                f"Warehouse '{self.name}' has no timezone-conversion rewrite, so "
                f"`column_timezone` cannot be honored. Store the column in the "
                f"target timezone, or drop `column_timezone` from the time binding."
            ),
            details={
                "warehouse": self.name,
                "rewrite": "convert_timezone",
                "source_timezone": source_tz,
                "target_timezone": target_tz,
            },
        )

    def date_diff(self, unit: str, start_expr: Any, end_expr: Any) -> Any:
        return SqlCall(
            "DATE_DIFF",
            [SqlLiteral(unit), self.timestamp_cast(start_expr), self.timestamp_cast(end_expr)],
        )

    def date_add(self, unit: str, value_expr: Any, date_expr: Any) -> Any:
        return SqlCall("DATE_ADD", [date_expr, SqlInterval(value_expr, unit)])

    def null_safe_eq(self, left: Any, right: Any) -> Any:
        return SqlBinary(left, "IS NOT DISTINCT FROM", right)

    def single_value(self, expr: Any, *, boolean: bool = False) -> Any:
        """A group's one value of ``expr``, NULL where its known values differ: a one-per-key
        model's column. ``boolean`` marks a column a dimension declares true/false."""
        names = self.boolean_bounds if boolean else ("MIN", "MAX")
        least, most = (SqlCall(name, [expr]) for name in names)
        return SqlCase([SqlCaseWhen(SqlBinary(least, "=", most), most)])

    @property
    def outer_lookup_joins(self) -> bool:
        """Whether a many-to-one lookup may be a LEFT JOIN, keeping rows it finds no match for.

        It needs an unmatched row to read NULL from the looked-up columns. A dialect whose
        outer join fills them with a type default instead says False, and lookups stay inner.
        """
        return True

    @property
    def subquery_in_join_on(self) -> bool:
        """Whether a join's ON may hold an uncorrelated ``IN (SELECT …)``, keeping a LEFT
        join's unmatched rows. Each warehouse declares it once its tests prove it."""
        return False

    @property
    def has_implicit_calendar(self) -> bool:
        return type(self).day_series is not SqlDialect.day_series

    def day_series(self, start: Any, end: Any, source: str) -> SqlSelect:
        """Every day from ``start`` through ``end`` (both included) as a DATE ``date_day``.

        ``start`` and ``end`` are DATE expressions over the one-row relation
        ``source``, which always holds finite bounds. This is the implicit
        calendar's spine; a warehouse without an override refuses every fill.
        """
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            f"Warehouse '{self.name}' has no implicit calendar, so time.fill, rolling and "
            "prior_period are not supported on it yet",
            details={"warehouse": self.name, "rewrite": "day_series"},
        )

    def conditional_aggregate(self, aggregation: str, condition: Any, value: Any | None) -> Any:
        """Emit ``<AGG>(CASE WHEN condition THEN value END)``.

        The portable default works on every SQL dialect. Subclasses may
        override to emit a native form — Snowflake's ``COUNT_IF`` /
        ``SUM_IF``, BigQuery's ``COUNTIF``, Postgres-style
        ``<AGG>(value) FILTER (WHERE condition)``, etc. The hook
        receives the already-lowered ``condition`` and ``value`` SQL AST
        nodes so subclasses can compose freely without re-walking the
        semantic AST. ``value`` is ``None`` for the ``count`` shape
        (``aggregate_if(count, cond)``); subclasses should treat
        ``COUNT(CASE WHEN cond THEN 1 END)`` as the canonical fallback.
        """
        body = value if value is not None else SqlLiteral(1)
        case_expr = SqlCase(
            whens=[SqlCaseWhen(condition=condition, result=body)],
            else_expr=None,
        )
        return SqlCall(aggregation.upper(), [case_expr])

    def json_extract(self, expr: Any, path: list[str], *, as_text: bool) -> Any:
        """The JSON value at the object keys ``path`` inside ``expr``; text when ``as_text``."""
        function = "JSON_EXTRACT_STRING" if as_text else "JSON_EXTRACT"
        return SqlCall(function, [expr, SqlLiteral("$." + ".".join(path))])

    def explode(
        self, expr: Any, delimiter: str, *, alias: str, column: str
    ) -> tuple[SqlTableFunction, Any]:
        """A table function named ``alias`` with one row per element of the list ``expr`` (split
        on ``delimiter`` when one is given), and the expression that reads the element."""
        if delimiter:
            expr = SqlCall("STRING_SPLIT", [expr, SqlLiteral(delimiter)])
        table = SqlTableFunction("UNNEST", args=[expr], alias=alias, columns=[column])
        return table, SqlIdentifier(parts=[alias, column])

    def capabilities(self) -> dict[str, Any]:
        return {
            "date_trunc": True,
            "date_diff": True,
            "null_safe_equality": True,
            "percentile_cont": True,
            "median": True,
            "snapshot_row_selection": "aggregate_join",
            "qualify": False,
            "timestamp_type": self.timestamp_type_name(),
        }


class _OrderedSetPercentile:
    def percentile_cont(self, expr: Any, percentile: float) -> Any:
        return SqlWithinGroup(
            SqlCall("PERCENTILE_CONT", [SqlLiteral(percentile)]),
            order_by=[SqlOrderTerm(expr=expr, direction="ASC")],
        )


class _ConvertTimezone:
    def convert_timezone(self, source_tz: str, target_tz: str, ts_expr: Any) -> Any:
        # Native: CONVERT_TIMEZONE(sourceTz, targetTz, ts). This spelling
        # used to be the base-class default for every warehouse, which is
        # why the other dialects needed correcting rather than adding.
        return SqlCall("CONVERT_TIMEZONE", [SqlLiteral(source_tz), SqlLiteral(target_tz), ts_expr])


class _NestedTimezone:
    reads_coverage = True
    exists_in_select = True

    def utc_timestamp(self, expr: Any, storage_zone: str) -> Any:
        """UTC wall time for an instant, or for naive storage in its declared zone.

        Package timestamp metadata does not distinguish these physical SQL types.
        Both CASE arms return naive UTC, so session coercion cannot change the cutoff.
        """
        aware = SqlBinary(
            SqlCast(SqlCall("PG_TYPEOF", [expr]), "VARCHAR"),
            "=",
            SqlLiteral("timestamp with time zone"),
        )
        instant = SqlCall("TIMEZONE", [SqlLiteral("UTC"), SqlCast(expr, "TIMESTAMPTZ")])
        naive = self.convert_timezone(storage_zone, "UTC", self.timestamp_cast(expr))  # type: ignore[attr-defined]
        return SqlCase([SqlCaseWhen(aware, instant)], naive)

    def convert_timezone(self, source_tz: str, target_tz: str, ts_expr: Any) -> Any:
        # `timezone(tz, naive_ts)` reads the naive value as wall-clock in
        # `tz` and yields an instant; `timezone(tz, instant)` renders an
        # instant back to naive wall-clock in `tz`. Nesting them therefore
        # reinterprets source -> target. Verified on DuckDB 1.5.3:
        # 2024-01-15 12:00 UTC -> 2024-01-15 07:00 America/New_York.
        return SqlCall(
            "timezone",
            [SqlLiteral(target_tz), SqlCall("timezone", [SqlLiteral(source_tz), ts_expr])],
        )


@dataclass(frozen=True)
class SnowflakeDialect(_OrderedSetPercentile, _ConvertTimezone, SqlDialect):
    name: str = "snowflake"
    reads_coverage = True
    boolean_bounds = ("BOOLAND_AGG", "BOOLOR_AGG")
    call_names = PORTABLE_CALL_NAMES | {"DATE_PART", "DATE_TRUNC", "LEFT", "RIGHT", "SPLIT"}
    # A column's own collation is set aside: COLLATE(x, '') is x with none, both sides alike.
    case_fold = "LOWER(COLLATE({}, ''))"
    date_diff_units = tuple(unit for unit in CONVERSION_WINDOW_UNITS if unit != "week")

    def prepare_query(self, sql: str) -> PreparedQuery:
        return PreparedQuery(float_nullif_divisions(sql))

    def quote_string_literal(self, value: str) -> str:
        return backslash_escaped_string_literal(value)

    # MIN_BY/MAX_BY ignore a NULL key but return the value at the edge even when it is NULL
    # (docs.snowflake.com/en/sql-reference/functions/min_by), and Snowflake has no aggregate
    # FILTER: a NULL value's key is NULL too, so its row is skipped as well.
    def first_value(self, expr: Any, order_expr: Any) -> Any:
        return SqlCall("MIN_BY", [expr, _null_where_null(expr, order_expr)])

    def last_value(self, expr: Any, order_expr: Any) -> Any:
        return SqlCall("MAX_BY", [expr, _null_where_null(expr, order_expr)])

    def timestamp_type_name(self) -> str:
        return "TIMESTAMP_NTZ"

    def date_diff(self, unit: str, start_expr: Any, end_expr: Any) -> Any:
        return SqlCall(
            "DATEDIFF",
            [SqlLiteral(unit), self.timestamp_cast(start_expr), self.timestamp_cast(end_expr)],
        )

    def date_add(self, unit: str, value_expr: Any, date_expr: Any) -> Any:
        return SqlCall("DATEADD", [SqlDatePart(unit), value_expr, date_expr])

    def day_series(self, start: Any, end: Any, source: str) -> SqlSelect:
        # ARRAY_GENERATE_RANGE stops before its end, hence the + 1.
        count = SqlBinary(self.date_diff("day", start, end), "+", SqlLiteral(1))
        offsets = SqlCall("ARRAY_GENERATE_RANGE", [SqlLiteral(0), count])
        series = SqlTableFunction(
            "FLATTEN", named_args=[SqlNamedArg("INPUT", offsets)], alias="day_series", lateral=True
        )
        offset = SqlCast(SqlIdentifier(parts=["day_series", "VALUE"]), "INTEGER")
        return _day_rows(self.date_add("day", offset, start), source, series)

    def null_safe_eq(self, left: Any, right: Any) -> Any:
        return SqlCall("EQUAL_NULL", [left, right])

    def json_extract(self, expr: Any, path: list[str], *, as_text: bool) -> Any:
        return SqlJsonExtract(expr, path=path, as_text=as_text)

    def explode(
        self, expr: Any, delimiter: str, *, alias: str, column: str
    ) -> tuple[SqlTableFunction, Any]:
        if delimiter:
            expr = SqlCall("SPLIT", [expr, SqlLiteral(delimiter)])
        table = SqlTableFunction(
            "FLATTEN", named_args=[SqlNamedArg("INPUT", expr)], alias=alias, lateral=True
        )
        return table, SqlIdentifier(parts=[alias, "VALUE"])

    def conditional_aggregate(self, aggregation: str, condition: Any, value: Any | None) -> Any:
        agg = aggregation.lower()
        if agg == "count" and value is None:
            return SqlCall("COUNT_IF", [condition])
        # NOTE: Snowflake has no SUM_IF — the previous native lowering here
        # raised "Unknown function SUM_IF" live and was caught by the
        # cross-warehouse conformance suite. The sum shape falls back to the
        # portable CASE form, which Snowflake executes natively.
        return super().conditional_aggregate(aggregation, condition, value)

    def capabilities(self) -> dict[str, Any]:
        payload = super().capabilities()
        payload.update(
            {
                "qualify": True,
                "null_safe_equality_function": "EQUAL_NULL",
                "conditional_aggregate_native": True,
            }
        )
        return payload


@dataclass(frozen=True)
class DuckDbDialect(_NestedTimezone, SqlDialect):
    subquery_in_join_on = True
    name: str = "duckdb"
    session_zone = True
    call_names = PORTABLE_CALL_NAMES | {
        "DATE_PART",
        "DATE_TRUNC",
        "LEFT",
        "RIGHT",
        "JSON_EXTRACT",
        "JSON_EXTRACT_STRING",
        "SPLIT",
        "STRING_SPLIT",
        "STR_SPLIT",
    }

    def day_series(self, start: Any, end: Any, source: str) -> SqlSelect:
        # GENERATE_SERIES includes its end; over DATEs it yields midnight TIMESTAMPs.
        series = SqlTableFunction(
            "GENERATE_SERIES",
            args=[start, end, SqlInterval(SqlLiteral(1), "day")],
            alias="day_series",
            columns=["series_day"],
            lateral=True,
        )
        day = SqlCast(SqlIdentifier(parts=["day_series", "series_day"]), "DATE")
        return _day_rows(day, source, series)

    def naive_date_clock(self, expr: Any) -> Any:
        return self.timestamp_cast(expr)


@dataclass(frozen=True)
class PostgresDialect(_OrderedSetPercentile, _NestedTimezone, SqlDialect):
    """PostgreSQL (ADBC, ``postgres_native``).

    Quirks covered here (each verified against PostgreSQL 16):

    - No ``MEDIAN`` / ``QUANTILE_CONT`` — both lower to the ordered-set
      aggregate ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY expr ASC)``
      (linear interpolation, matching DuckDB ``QUANTILE_CONT``).
    - No ``ARG_MIN`` / ``ARG_MAX`` — ``first_value`` / ``last_value``
      lower to ``(ARRAY_AGG(expr ORDER BY order))[1]``.
    - No ``DATE_DIFF`` — boundary-crossing differences are rebuilt per
      unit from ``DATE_PART`` / ``DATE_TRUNC`` / date subtraction so the
      semantics match DuckDB's ``DATE_DIFF`` exactly.
    - No ``DATE_ADD`` and no renderable ``INTERVAL`` literal — interval
      addition is rebuilt from timestamp arithmetic (fixed-length units)
      or calendar reconstruction with day clamping (month-ish units).
    - ``IS NOT DISTINCT FROM``, ``TIMESTAMP``, and the portable
      ``<AGG>(CASE WHEN …)`` conditional-aggregate default are native.
    """

    subquery_in_join_on = True

    name: str = "postgres"
    session_zone = True
    call_names = PORTABLE_CALL_NAMES | {"DATE_PART", "DATE_TRUNC", "LEFT", "RIGHT"}
    # LOWER folds by its operand's collation, and a column's own collation can differ from a
    # literal's: both read as text (an enum or char column too) and fold under the ICU root
    # collation, built in wherever Postgres has ICU; without it the query fails rather than
    # folding the two apart.
    case_fold = 'LOWER(CAST({} AS TEXT) COLLATE "und-x-icu")'

    def day_series(self, start: Any, end: Any, source: str) -> SqlSelect:
        # Integer day offsets (DATE - DATE is an integer, DATE + integer a DATE), since
        # there is no renderable INTERVAL literal here; GENERATE_SERIES includes its end.
        series = SqlTableFunction(
            "GENERATE_SERIES",
            args=[SqlLiteral(0), SqlBinary(end, "-", start)],
            alias="day_series",
            columns=["day_offset"],
            lateral=True,
        )
        day = SqlBinary(start, "+", SqlIdentifier(parts=["day_series", "day_offset"]))
        return _day_rows(day, source, series)

    def naive_date_clock(self, expr: Any) -> Any:
        return self.timestamp_cast(expr)

    # Fixed-length units expressed as a timestamp difference (PG renders
    # no bare INTERVAL literal through this AST, but `ts2 - ts1` IS an
    # interval, so `value * (ts2 - ts1)` scales it exactly).
    _FIXED_UNIT_BOUNDS = {
        "second": ("2000-01-01 00:00:01", "2000-01-01 00:00:00"),
        "minute": ("2000-01-01 00:01:00", "2000-01-01 00:00:00"),
        "hour": ("2000-01-01 01:00:00", "2000-01-01 00:00:00"),
        "day": ("2000-01-02", "2000-01-01"),
        "week": ("2000-01-08", "2000-01-01"),
        "isoweek": ("2000-01-08", "2000-01-01"),
    }

    _MONTHS_PER_UNIT = {"month": 1, "quarter": 3, "year": 12}

    def prepare_query(self, sql: str) -> PreparedQuery:
        sql, alias_map = postgres_compat_sql(sql)
        return PreparedQuery(sql, tuple(alias_map.items()))

    def cast_type_name(self, logical_type: str) -> str:
        return "FLOAT8" if logical_type == "DOUBLE" else super().cast_type_name(logical_type)

    def median(self, expr: Any) -> Any:
        # PG has no MEDIAN; PERCENTILE_CONT(0.5) is the exact equivalent.
        return self.percentile_cont(expr, 0.5)

    def _value_by(self, expr: Any, order_expr: Any, direction: str) -> Any:
        # ARRAY_AGG keeps NULL values and sorts a NULL key last ascending but first
        # descending: FILTER keeps the rows whose value and key are both known.
        from .sql_ast import SqlCallWithOrder, SqlIndexAccess

        ordered = SqlCallWithOrder(
            SqlCall("ARRAY_AGG", [expr]),
            order_by=[SqlOrderTerm(expr=order_expr, direction=direction)],
        )
        return SqlIndexAccess(
            SqlAggregateFilter(ordered, _value_and_key_known(expr, order_expr)), SqlLiteral(1)
        )

    def first_value(self, expr: Any, order_expr: Any) -> Any:
        return self._value_by(expr, order_expr, "ASC")

    def last_value(self, expr: Any, order_expr: Any) -> Any:
        return self._value_by(expr, order_expr, "DESC")

    def single_value(self, expr: Any, *, boolean: bool = False) -> Any:
        # No MIN or MAX of a boolean, and a column only a measure reads has no declared type:
        # count the known distinct values, and read the one there is from an array of them
        # (never of every row).
        from .sql_ast import SqlIndexAccess

        distinct = SqlCall("ARRAY_AGG", [expr], distinct=True)
        known = SqlAggregateFilter(distinct, SqlBinary(expr, "IS NOT", SqlLiteral(None)))
        one = SqlBinary(SqlCall("COUNT", [expr], distinct=True), "=", SqlLiteral(1))
        return SqlCase([SqlCaseWhen(one, SqlIndexAccess(known, SqlLiteral(1)))])

    def _date_of(self, expr: Any) -> Any:
        return SqlCast(expr, "DATE")

    def _date_part(self, part: str, expr: Any) -> Any:
        return SqlCall("DATE_PART", [SqlLiteral(part), expr])

    def date_diff(self, unit: str, start_expr: Any, end_expr: Any) -> Any:
        """Boundary-crossing date difference matching DuckDB ``DATE_DIFF``.

        PG has no DATEDIFF; each unit counts how many unit *boundaries*
        lie between start and end (NOT elapsed time), exactly like
        DuckDB/Snowflake:

        - day:   ``end::date - start::date`` (integer days)
        - week:  day-difference of the ISO Monday week truncations / 7
        - month/quarter/year: DATE_PART arithmetic on calendar fields
        - hour/minute/second: epoch difference of the unit truncations
        """
        normalized = str(unit or "").strip().lower()
        start_ts = self.timestamp_cast(start_expr)
        end_ts = self.timestamp_cast(end_expr)
        if normalized == "day":
            return SqlBinary(self._date_of(end_ts), "-", self._date_of(start_ts))
        if normalized in ("week", "isoweek"):
            # DuckDB's date_diff('week', …) is NOT a boundary count: it
            # is the day difference divided by 7, truncated toward zero
            # (verified by probe). PG integer division also truncates
            # toward zero, so `(end::date - start::date) / 7` is exact.
            day_delta = SqlBinary(self._date_of(end_ts), "-", self._date_of(start_ts))
            return SqlBinary(day_delta, "/", SqlLiteral(7))
        if normalized in ("month", "quarter", "year"):
            year_delta = SqlBinary(
                self._date_part("year", end_ts), "-", self._date_part("year", start_ts)
            )
            if normalized == "year":
                return SqlCast(year_delta, "BIGINT")
            per_year = 12 if normalized == "month" else 4
            sub_delta = SqlBinary(
                self._date_part(normalized, end_ts), "-", self._date_part(normalized, start_ts)
            )
            return SqlCast(
                SqlBinary(SqlBinary(year_delta, "*", SqlLiteral(per_year)), "+", sub_delta),
                "BIGINT",
            )
        if normalized in ("hour", "minute", "second"):

            def _epoch_of_trunc(ts: Any) -> Any:
                return self._date_part("epoch", SqlCall("DATE_TRUNC", [SqlLiteral(normalized), ts]))

            delta = SqlBinary(_epoch_of_trunc(end_ts), "-", _epoch_of_trunc(start_ts))
            divisor = {"hour": 3600, "minute": 60, "second": 1}[normalized]
            if divisor == 1:
                return SqlCast(delta, "BIGINT")
            # Exact: truncated epochs are multiples of the divisor.
            return SqlCast(SqlBinary(delta, "/", SqlLiteral(divisor)), "BIGINT")
        return super().date_diff(unit, start_expr, end_expr)

    def _one_unit_interval(self, unit: str) -> Any:
        upper, lower = self._FIXED_UNIT_BOUNDS[unit]
        return SqlBinary(
            SqlCast(SqlLiteral(upper), "TIMESTAMP"), "-", SqlCast(SqlLiteral(lower), "TIMESTAMP")
        )

    def _month_index(self, ts: Any, months_offset: Any) -> Any:
        """``year*12 + (month-1) + offset`` — a linear month index."""
        return SqlBinary(
            SqlBinary(self._date_part("year", ts), "*", SqlLiteral(12)),
            "+",
            SqlBinary(
                SqlBinary(self._date_part("month", ts), "-", SqlLiteral(1)), "+", months_offset
            ),
        )

    def _month_first_day(self, month_index: Any) -> Any:
        """First day of the month at ``month_index`` as a timestamp.

        PG renders no INTERVAL literal through this AST, so the date is
        rebuilt as text: ``CAST(CONCAT(year, '-', month, '-01') AS
        TIMESTAMP)``. FLOOR keeps negative offsets correct (floor
        division, not truncation).
        """
        year = SqlCast(SqlCall("FLOOR", [SqlBinary(month_index, "/", SqlLiteral(12))]), "INTEGER")
        month = SqlCast(
            SqlBinary(
                SqlBinary(
                    month_index,
                    "-",
                    SqlBinary(
                        SqlCall("FLOOR", [SqlBinary(month_index, "/", SqlLiteral(12))]),
                        "*",
                        SqlLiteral(12),
                    ),
                ),
                "+",
                SqlLiteral(1),
            ),
            "INTEGER",
        )
        return SqlCast(
            SqlCall("CONCAT", [year, SqlLiteral("-"), month, SqlLiteral("-01")]), "TIMESTAMP"
        )

    def date_add(self, unit: str, value_expr: Any, date_expr: Any) -> Any:
        """Interval addition without INTERVAL literals (PG syntax gap).

        ``INTERVAL (n) MONTH`` is a syntax error in PostgreSQL (the
        parenthesized form is reserved for precision), so the portable
        ``DATE_ADD(date, INTERVAL (n) UNIT)`` cannot render. Instead:

        - Fixed-length units (day/week/hour/minute/second) become
          ``ts + value * (ts_hi - ts_lo)`` where ``ts_hi - ts_lo`` is
          exactly one unit (timestamp subtraction IS an interval).
        - month/quarter/year rebuild the target calendar month and clamp
          the day-of-month (Jan 31 + 1 month = Feb 29/28), matching
          native ``+ INTERVAL`` semantics on PG and DuckDB, preserving
          the time of day.
        """
        normalized = str(unit or "").strip().lower()
        ts = self.timestamp_cast(date_expr)
        if normalized in self._FIXED_UNIT_BOUNDS:
            return SqlBinary(
                ts, "+", SqlBinary(value_expr, "*", self._one_unit_interval(normalized))
            )
        if normalized in self._MONTHS_PER_UNIT:
            months_per = self._MONTHS_PER_UNIT[normalized]
            offset = (
                value_expr
                if months_per == 1
                else SqlBinary(value_expr, "*", SqlLiteral(months_per))
            )
            target_first = self._month_first_day(self._month_index(ts, offset))
            next_first = self._month_first_day(
                self._month_index(ts, SqlBinary(offset, "+", SqlLiteral(1)))
            )
            days_in_target = SqlBinary(self._date_of(next_first), "-", self._date_of(target_first))
            day_of_month = self._date_part("day", ts)
            clamped_day_offset = SqlCase(
                whens=[
                    SqlCaseWhen(
                        condition=SqlBinary(day_of_month, "<=", days_in_target),
                        result=SqlBinary(day_of_month, "-", SqlLiteral(1)),
                    )
                ],
                else_expr=SqlBinary(days_in_target, "-", SqlLiteral(1)),
            )
            time_of_day = SqlBinary(ts, "-", SqlCast(self._date_of(ts), "TIMESTAMP"))
            return SqlBinary(
                SqlBinary(
                    target_first,
                    "+",
                    SqlBinary(clamped_day_offset, "*", self._one_unit_interval("day")),
                ),
                "+",
                time_of_day,
            )
        return super().date_add(unit, value_expr, date_expr)

    def null_safe_eq(self, left: Any, right: Any) -> Any:
        """Null-safe equality that stays FULL JOIN-compatible on PG.

        ``IS NOT DISTINCT FROM`` alone fails with "FULL JOIN is only
        supported with merge-joinable or hash-joinable join
        conditions" — see :func:`hash_joinable_null_safe_eq` for the
        shared workaround (PG's text type is ``TEXT``).
        """
        return hash_joinable_null_safe_eq(left, right, text_cast_type="TEXT")

    def capabilities(self) -> dict[str, Any]:
        payload = super().capabilities()
        payload.update(
            {
                "null_safe_equality_function": "IS NOT DISTINCT FROM",
                "percentile_cont_within_group": True,
            }
        )
        return payload


@dataclass(frozen=True)
class BigQueryDialect(SqlDialect):
    """Google BigQuery (Standard SQL).

    Known quirks the implementation must cover: ``DATE_TRUNC(ts, unit)``
    and ``TIMESTAMP_DIFF(end, start, unit)`` reverse the portable
    argument order; ``PERCENTILE_CONT`` is window-only so percentile
    aggregates lower to ``APPROX_QUANTILES``.

    The fixture/loader convention stores naive timestamps as
    ``DATETIME``, so every temporal function uses the ``DATETIME_*``
    family (which, unlike ``TIMESTAMP_*``, supports MONTH/QUARTER/YEAR
    arithmetic).
    """

    like_escape_clause = False
    reads_coverage = True
    boolean_bounds = ("LOGICAL_AND", "LOGICAL_OR")
    call_names = PORTABLE_CALL_NAMES | {"LEFT", "RIGHT", "JSON_EXTRACT", "SPLIT"}
    # A column's own collation is set aside: COLLATE(x, '') is x with none, both sides alike.
    case_fold = "LOWER(COLLATE({}, ''))"
    date_diff_units = tuple(unit for unit in CONVERSION_WINDOW_UNITS if unit != "week")
    decimal_cast = False

    name: str = "bigquery"

    def day_series(self, start: Any, end: Any, source: str) -> SqlSelect:
        # GENERATE_DATE_ARRAY includes its end and steps one day by default.
        series = SqlTableFunction(
            "UNNEST", args=[SqlCall("GENERATE_DATE_ARRAY", [start, end])], alias="series_day"
        )
        return _day_rows(SqlIdentifier(parts=["series_day"]), source, series)

    def convert_timezone(self, source_tz: str, target_tz: str, ts_expr: Any) -> Any:
        # BigQuery has no CONVERT_TIMEZONE. DATETIME(timestamp, tz) renders an instant as
        # naive wall time in the target zone, matching the DATETIME_* family the rest of this
        # dialect uses. Stored in UTC, the value becomes its instant through CAST AS TIMESTAMP,
        # which reads a DATETIME or a DATE in UTC and keeps a TIMESTAMP as it is, so the one
        # form serves a DATETIME, DATE or TIMESTAMP column (an instant column declares UTC).
        # Stored as wall time in another zone, the value is a DATETIME or a DATE, and
        # TIMESTAMP(value, tz) reads it in that zone; TIMESTAMP(...) takes no TIMESTAMP.
        if source_tz == "UTC":
            instant: Any = SqlCast(ts_expr, "TIMESTAMP")
        else:
            instant = SqlCall("TIMESTAMP", [ts_expr, SqlLiteral(source_tz)])
        return SqlCall("DATETIME", [instant, SqlLiteral(target_tz)])

    def prepare_query(self, sql: str) -> PreparedQuery:
        sql, alias_map = bigquery_compat_sql(sql)
        return PreparedQuery(sql, tuple(alias_map.items()))

    def call_date_diff(self, unit: str, start_expr: Any, end_expr: Any) -> Any:
        # Force calendar boundaries in UTC, including TIMESTAMP endpoints; unwrapped
        # TIMESTAMPs select BigQuery's elapsed-duration overload.
        return self.date_diff(unit, SqlCast(start_expr, "DATETIME"), SqlCast(end_expr, "DATETIME"))

    def cast_type_name(self, logical_type: str) -> str:
        # DECIMAL never reaches here: decimal_cast is False.
        base = logical_type.split("(")[0]
        return {"DOUBLE": "FLOAT64", "INTEGER": "INT64", "BIGINT": "INT64", "VARCHAR": "STRING"}[
            base
        ]

    def quote_string_literal(self, value: str) -> str:
        return backslash_quoted_string_literal(value)

    def percentile_cont(self, expr: Any, percentile: float) -> Any:
        """EXACT linear-interpolation percentile (DuckDB QUANTILE_CONT
        parity) as an aggregate expression.

        ``PERCENTILE_CONT`` is window-only in BigQuery and
        ``APPROX_QUANTILES`` is approximate (it returns existing
        elements, no interpolation — verified live: p80 over small
        groups deviates from the DuckDB reference). Rebuild the exact
        definition from aggregates instead. With ``v`` the values
        sorted ascending, ``n = COUNT(v)`` and ``h = p * (n - 1)``::

            v[FLOOR(h)] + (h - FLOOR(h)) * (v[CEIL(h)] - v[FLOOR(h)])

        via ``(ARRAY_AGG(expr IGNORE NULLS ORDER BY expr ASC))[SAFE_OFFSET(...)]``
        array references. When ``h`` is integral FLOOR = CEIL and the fraction
        term is 0, so the result is the element itself. NULLs are left out, as
        DuckDB's QUANTILE_CONT leaves them out (an array holding NULL is an error
        on BigQuery), and a group with no value reads NULL: its position is
        negative, which SAFE_OFFSET reads as NULL.
        """
        from .sql_ast import SqlCallWithOrder, SqlIndexAccess

        def sorted_values() -> Any:
            return SqlCallWithOrder(
                call=SqlCall("ARRAY_AGG", [expr]),
                order_by=[SqlOrderTerm(expr=expr, direction="ASC")],
                ignore_nulls=True,
            )

        def position() -> Any:
            # h = p * (COUNT(expr) - 1); COUNT(expr) is ARRAY_AGG's element
            # count (both leave NULLs out).
            return SqlBinary(
                SqlLiteral(float(percentile)),
                "*",
                SqlBinary(SqlCall("COUNT", [expr]), "-", SqlLiteral(1)),
            )

        def element_at(index_call: str) -> Any:
            return SqlIndexAccess(
                expr=sorted_values(),
                index=SqlCast(SqlCall(index_call, [position()]), "BIGINT"),
                wrapper="SAFE_OFFSET",
            )

        lower = element_at("FLOOR")
        upper = element_at("CEIL")
        fraction = SqlBinary(position(), "-", SqlCall("FLOOR", [position()]))
        return SqlBinary(lower, "+", SqlBinary(fraction, "*", SqlBinary(upper, "-", lower)))

    def median(self, expr: Any) -> Any:
        return self.percentile_cont(expr, 0.5)

    def _array_agg_edge(self, expr: Any, order_expr: Any, direction: str) -> Any:
        # No MIN_BY/MAX_BY: ARRAY_AGG(expr IGNORE NULLS ORDER BY o … LIMIT 1)
        # [SAFE_OFFSET(0)], the first non-NULL value (an array holding NULL is an
        # error on BigQuery), or NULL for a group with none. A row with no key reads NULL
        # too: BigQuery sorts NULL first ascending, and IGNORE NULLS applies before ORDER BY
        # and LIMIT (aggregate function calls, GoogleSQL reference).
        from .sql_ast import SqlCallWithOrder, SqlIndexAccess

        return SqlIndexAccess(
            expr=SqlCallWithOrder(
                call=SqlCall("ARRAY_AGG", [_null_where_null(order_expr, expr)]),
                order_by=[SqlOrderTerm(expr=order_expr, direction=direction)],
                limit=1,
                ignore_nulls=True,
            ),
            index=SqlLiteral(0),
            wrapper="SAFE_OFFSET",
        )

    def first_value(self, expr: Any, order_expr: Any) -> Any:
        return self._array_agg_edge(expr, order_expr, "ASC")

    def last_value(self, expr: Any, order_expr: Any) -> Any:
        return self._array_agg_edge(expr, order_expr, "DESC")

    def timestamp_type_name(self) -> str:
        # Naive (zone-less) timestamps are DATETIME in BigQuery; the
        # fixture loader stores timestamp columns as DATETIME.
        return "DATETIME"

    def timestamp_cast(self, expr: Any) -> Any:
        # No cast: BigQuery's implicit coercions suffice here. DATE ->
        # DATETIME and string literal -> DATETIME both coerce wherever
        # the DATETIME_* functions and comparisons expect a DATETIME.
        return expr

    def date_trunc(self, grain: str, ts_expr: Any) -> Any:
        # BigQuery reverses the portable order: DATETIME_TRUNC(ts, unit)
        # with the unit as a keyword. 'week' maps to ISOWEEK (Monday
        # start) for parity with DuckDB's ISO-8601 date_trunc('week').
        part = "isoweek" if str(grain).strip().lower() == "week" else str(grain)
        return SqlCall("DATETIME_TRUNC", [self.timestamp_cast(ts_expr), SqlDatePart(part)])

    def date_diff(self, unit: str, start_expr: Any, end_expr: Any) -> Any:
        # BigQuery reverses the portable order: DATETIME_DIFF(end,
        # start, unit) — END FIRST. Week boundaries differ from DuckDB,
        # so authored week calls are refused by the scalar-call guard.
        return SqlCall(
            "DATETIME_DIFF",
            [
                self.timestamp_cast(end_expr),
                self.timestamp_cast(start_expr),
                SqlDatePart(unit),
            ],
        )

    def date_add(self, unit: str, value_expr: Any, date_expr: Any) -> Any:
        # DATETIME_ADD(dt, INTERVAL (n) UNIT); BigQuery accepts the
        # parenthesized interval value the SqlInterval node renders.
        return SqlCall("DATETIME_ADD", [date_expr, SqlInterval(value_expr, unit)])

    def conditional_aggregate(self, aggregation: str, condition: Any, value: Any | None) -> Any:
        if aggregation.lower() == "count" and value is None:
            return SqlCall("COUNTIF", [condition])
        return super().conditional_aggregate(aggregation, condition, value)

    def null_safe_eq(self, left: Any, right: Any) -> Any:
        """Null-safe equality that stays FULL JOIN-compatible on BigQuery.

        ``IS NOT DISTINCT FROM`` is valid GoogleSQL but BigQuery rejects
        FULL OUTER JOINs whose condition has no conjunct that is a plain
        equality of fields from both sides ("FULL OUTER JOIN cannot be
        used without a condition that is an equality of fields…") — see
        :func:`hash_joinable_null_safe_eq` for the shared workaround
        (GoogleSQL's text type is ``STRING``).
        """
        return hash_joinable_null_safe_eq(left, right, text_cast_type="STRING")

    def capabilities(self) -> dict[str, Any]:
        payload = super().capabilities()
        payload.update(
            {
                "conditional_aggregate_native": True,
                "null_safe_equality_function": "IS NOT DISTINCT FROM",
                "percentile_cont_exact": True,
                "percentile_cont_method": (
                    "exact linear interpolation over (ARRAY_AGG(expr IGNORE NULLS ORDER BY "
                    "expr))[SAFE_OFFSET(FLOOR/CEIL(p * (COUNT(expr) - 1)))] — PERCENTILE_CONT "
                    "is window-only in BigQuery, so the exact DuckDB QUANTILE_CONT definition "
                    "is rebuilt from aggregates; NULLs are left out."
                ),
                "median_exact": True,
                "first_last_value_method": (
                    "(ARRAY_AGG(CASE WHEN order IS NULL THEN NULL ELSE expr END IGNORE NULLS "
                    "ORDER BY order LIMIT 1))[SAFE_OFFSET(0)] — the first value whose value and "
                    "order are both known, NULL when there is none."
                ),
            }
        )
        return payload


@dataclass(frozen=True)
class DatabricksDialect(_OrderedSetPercentile, _ConvertTimezone, SqlDialect):
    """Databricks / Spark SQL. Null-safe equality is ``<=>``.

    Parity notes (vs the DuckDB reference):

    - ``DATE_TRUNC('grain', ts)`` is valid Spark SQL and ``'week'``
      truncates to Monday (ISO-8601) — the portable default is kept.
    - ``TIMESTAMPDIFF``/``TIMESTAMPADD`` take the unit as a keyword
      (``SqlDatePart``), not a string literal.
    - ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY x)`` is supported on
      Databricks SQL and uses linear interpolation — same semantics as
      DuckDB's ``QUANTILE_CONT``.
    - ``MEDIAN`` and ``CONVERT_TIMEZONE(src, tgt, ts)`` match the
      portable defaults, so they are not overridden.
    """

    name: str = "databricks"
    reads_coverage = True
    call_names = PORTABLE_CALL_NAMES | {"DATE_PART", "DATE_TRUNC", "LEFT", "RIGHT", "SPLIT"}
    # A column's own collation (UNICODE, UTF8_LCASE) is set aside: LIKE reads binary text.
    case_fold = "LOWER(({}) COLLATE UTF8_BINARY)"

    def day_series(self, start: Any, end: Any, source: str) -> SqlSelect:
        # SEQUENCE over DATEs includes its end and steps one day by default.
        return _day_rows(SqlCall("EXPLODE", [SqlCall("SEQUENCE", [start, end])]), source)

    def prepare_query(self, sql: str) -> PreparedQuery:
        sql = rewrite_double_quoted_identifiers(sql, backslash=True)
        return PreparedQuery(float_nullif_divisions(sql, backslash=True))

    def cast_type_name(self, logical_type: str) -> str:
        return "STRING" if logical_type == "VARCHAR" else super().cast_type_name(logical_type)

    def quote_string_literal(self, value: str) -> str:
        return backslash_quoted_string_literal(value)

    # MIN_BY/MAX_BY return the value at the edge even when it is NULL; FILTER keeps the rows
    # whose value and key are both known (docs.databricks.com/en/sql/language-manual/
    # functions/min_by.html: min_by(expr, ordExpr) [FILTER (WHERE cond)]).
    def first_value(self, expr: Any, order_expr: Any) -> Any:
        known = _value_and_key_known(expr, order_expr)
        return SqlAggregateFilter(SqlCall("MIN_BY", [expr, order_expr]), known)

    def last_value(self, expr: Any, order_expr: Any) -> Any:
        known = _value_and_key_known(expr, order_expr)
        return SqlAggregateFilter(SqlCall("MAX_BY", [expr, order_expr]), known)

    def date_diff(self, unit: str, start_expr: Any, end_expr: Any) -> Any:
        """Boundary-crossing date difference matching DuckDB ``DATE_DIFF``.

        Spark's ``TIMESTAMPDIFF`` counts COMPLETE elapsed units
        (``23:00 → 01:00 next day`` is 0 days), while the portable
        contract (DuckDB, Snowflake ``DATEDIFF``, the Postgres
        relowering) counts unit-boundary CROSSINGS (that pair is 1
        day). Truncating BOTH operands to the unit before
        ``TIMESTAMPDIFF`` makes complete-unit counting equal to
        boundary counting, because the endpoints are boundary-aligned.

        ``week`` is the documented DuckDB exception (see the Postgres
        dialect): NOT a Monday-boundary count but the day delta divided
        by 7, truncated toward zero. Spark ``/`` is float division, so
        the quotient is cast to BIGINT (Spark casts truncate toward
        zero, matching DuckDB for negative deltas).
        """
        normalized = str(unit or "").strip().lower()
        if normalized == "isoweek":
            normalized = "week"

        def _aligned_diff(grain: str) -> Any:
            return SqlCall(
                "TIMESTAMPDIFF",
                [
                    SqlDatePart(grain),
                    self.date_trunc(grain, start_expr),
                    self.date_trunc(grain, end_expr),
                ],
            )

        if normalized == "week":
            return SqlCast(SqlBinary(_aligned_diff("day"), "/", SqlLiteral(7)), "BIGINT")
        return _aligned_diff(normalized)

    def date_add(self, unit: str, value_expr: Any, date_expr: Any) -> Any:
        return SqlCall("TIMESTAMPADD", [SqlDatePart(unit), value_expr, date_expr])

    def null_safe_eq(self, left: Any, right: Any) -> Any:
        return SqlBinary(left, "<=>", right)

    def conditional_aggregate(self, aggregation: str, condition: Any, value: Any | None) -> Any:
        if aggregation.lower() == "count" and value is None:
            return SqlCall("COUNT_IF", [condition])
        return super().conditional_aggregate(aggregation, condition, value)


@dataclass(frozen=True)
class MotherDuckDialect(DuckDbDialect):
    """MotherDuck — DuckDB SQL over the ``md:`` cloud transport."""

    name: str = "motherduck"


@dataclass(frozen=True)
class DuckLakeDialect(DuckDbDialect):
    """DuckLake — DuckDB SQL over an attached lakehouse catalog."""

    name: str = "ducklake"


@dataclass(frozen=True)
class AthenaDialect(SqlDialect):
    """AWS Athena (Trino SQL): exact array-based percentiles,
    ``date_diff('unit', start, end)``, ``IS NOT DISTINCT FROM``.

    The portable defaults already match Trino for ``date_trunc``
    (``DATE_TRUNC('month', CAST(.. AS TIMESTAMP))``), null-safe equality
    (``IS NOT DISTINCT FROM``), and the conditional aggregate CASE form.
    Native ``date_diff`` counts complete elapsed units, so authored
    portable ``DATE_DIFF`` calls are refused by the scalar-call guard.
    Overrides cover ``DATE_ADD('unit', n, ts)``
    (Trino rejects the INTERVAL form), ``MIN_BY``/``MAX_BY``, and the
    percentile family — Trino has NO exact ``PERCENTILE_CONT``
    aggregate and ``APPROX_PERCENTILE`` is a t-digest sketch, so
    percentiles lower to an EXACT linear interpolation over a sorted
    ``ARRAY_AGG`` (see :meth:`percentile_cont`).
    """

    name: str = "athena"
    reads_coverage = True
    boolean_bounds = ("BOOL_AND", "BOOL_OR")
    call_names = PORTABLE_CALL_NAMES | {"DATE_TRUNC", "JSON_EXTRACT", "SPLIT"}
    date_diff_units = ()  # its native date_diff counts complete elapsed units

    def day_series(self, start: Any, end: Any, source: str) -> SqlSelect:
        # SEQUENCE over DATEs includes its end and steps one day. Trino caps one
        # sequence at 10,000 elements (about 27 years) and fails loudly beyond that.
        series = SqlTableFunction(
            "UNNEST",
            args=[SqlCall("SEQUENCE", [start, end])],
            alias="day_series",
            columns=["series_day"],
        )
        return _day_rows(SqlIdentifier(parts=["day_series", "series_day"]), source, series)

    def prepare_query(self, sql: str) -> PreparedQuery:
        return PreparedQuery(float_nullif_divisions(sql, cast_type="DOUBLE"))

    def temporal_literal(self, value: str) -> str:
        # Trino refuses a timestamp compared with text (TYPE_MISMATCH) where DuckDB coerces the
        # text; a typed literal compares the same way, and a DATE operand coerces to it. Its
        # TIMESTAMP literal separates the date and the time with a space, never ISO's ``T``.
        if not _ISO_TEMPORAL.fullmatch(value):
            return self.quote_string_literal(value)
        return f"TIMESTAMP {self.quote_string_literal(value.replace('T', ' '))}"

    def convert_timezone(self, source_tz: str, target_tz: str, ts_expr: Any) -> Any:
        # Trino: with_timezone(naive_ts, tz) attaches a zone;
        # at_timezone(instant, tz) re-renders it in the target zone.
        return SqlCall(
            "at_timezone",
            [SqlCall("with_timezone", [ts_expr, SqlLiteral(source_tz)]), SqlLiteral(target_tz)],
        )

    def utc_timestamp(self, expr: Any, storage_zone: str) -> Any:
        # at_timezone returns a zoned value; casting drops the zone and keeps its UTC wall
        # time, so it compares with the naive ``now`` without the session zone.
        converted = super().utc_timestamp(expr, storage_zone)
        return converted if storage_zone == "UTC" else self.timestamp_cast(converted)

    def percentile_cont(self, expr: Any, percentile: float) -> Any:
        # Trino has no exact percentile aggregate (APPROX_PERCENTILE is
        # a t-digest sketch that deviates from PERCENTILE_CONT's linear
        # interpolation). Compose the exact value from a sorted array:
        #
        #   n    = COUNT(x)                  -- non-NULL count
        #   arr  = ARRAY_AGG(x ORDER BY x)   -- ASC NULLS LAST (Trino
        #                                       default), so positions
        #                                       1..n are the sorted
        #                                       non-NULL values
        #   pos  = p * (n - 1)               -- 0-based rank
        #   lo   = arr[CAST(FLOOR(pos) AS BIGINT) + 1]   -- 1-based
        #   hi   = arr[CAST(CEIL(pos)  AS BIGINT) + 1]
        #   frac = pos - FLOOR(pos)
        #   PERCENTILE_CONT = lo + (hi - lo) * frac
        #
        # For p in [0, 1] both indexes stay within [1, n]. The CASE
        # guard returns NULL on an empty (or all-NULL) input set —
        # matching PERCENTILE_CONT — instead of indexing arr[0].
        # Emitting ARRAY_AGG twice is fine: Trino deduplicates
        # identical aggregate expressions within a select.
        from .sql_ast import SqlCallWithOrder, SqlIndexAccess

        count = SqlCall("COUNT", [expr])
        position = SqlBinary(
            SqlLiteral(float(percentile)), "*", SqlBinary(count, "-", SqlLiteral(1))
        )
        floor_position = SqlCall("FLOOR", [position])

        def _sorted_element(zero_based_index: Any) -> Any:
            return SqlIndexAccess(
                expr=SqlCallWithOrder(
                    call=SqlCall("ARRAY_AGG", [expr]),
                    order_by=[SqlOrderTerm(expr=expr, direction="ASC")],
                ),
                index=SqlBinary(SqlCast(zero_based_index, "BIGINT"), "+", SqlLiteral(1)),
            )

        lower_value = _sorted_element(floor_position)
        upper_value = _sorted_element(SqlCall("CEIL", [position]))
        fraction = SqlBinary(position, "-", floor_position)
        interpolated = SqlBinary(
            lower_value,
            "+",
            SqlBinary(SqlBinary(upper_value, "-", lower_value), "*", fraction),
        )
        return SqlCase(
            whens=[
                SqlCaseWhen(
                    condition=SqlBinary(count, ">", SqlLiteral(0)),
                    result=interpolated,
                )
            ],
            else_expr=None,
        )

    def median(self, expr: Any) -> Any:
        return self.percentile_cont(expr, 0.5)

    # Trino's min_by/max_by don't ignore NULLs as other aggregates do (trino.io/docs/current/
    # functions/aggregate.html); FILTER, which every Trino aggregate takes, keeps the rows whose
    # value and key are both known.
    def first_value(self, expr: Any, order_expr: Any) -> Any:
        known = _value_and_key_known(expr, order_expr)
        return SqlAggregateFilter(SqlCall("MIN_BY", [expr, order_expr]), known)

    def last_value(self, expr: Any, order_expr: Any) -> Any:
        known = _value_and_key_known(expr, order_expr)
        return SqlAggregateFilter(SqlCall("MAX_BY", [expr, order_expr]), known)

    def date_add(self, unit: str, value_expr: Any, date_expr: Any) -> Any:
        # Trino form: DATE_ADD('unit', value, ts) — unit as a string
        # literal first. The portable INTERVAL form is rejected by Trino.
        return SqlCall("DATE_ADD", [SqlLiteral(unit), value_expr, date_expr])

    def capabilities(self) -> dict[str, Any]:
        payload = super().capabilities()
        payload.update(
            {
                # Percentiles/medians are EXACT: linear interpolation
                # over a sorted ARRAY_AGG (Trino has no PERCENTILE_CONT
                # aggregate; APPROX_PERCENTILE is not used because its
                # t-digest sketch deviates from exact interpolation).
                "percentile_exact": True,
                "median_exact": True,
                "percentile_cont_method": (
                    "sorted ARRAY_AGG linear interpolation — "
                    "arr[floor(p*(n-1))+1] + (arr[ceil(p*(n-1))+1] - "
                    "arr[floor(p*(n-1))+1]) * frac; exact DuckDB "
                    "QUANTILE_CONT parity. Materializes the group's "
                    "values in memory; very large groups may prefer "
                    "APPROX_PERCENTILE."
                ),
            }
        )
        return payload


@dataclass(frozen=True)
class ClickHouseDialect(SqlDialect):
    """ClickHouse: ``quantile*`` aggregates, its own date functions,
    null-safe equality via ``<=>`` / isNotDistinctFrom.

    Quirks (verified live on ClickHouse 24.8):

    - ``quantileExactInclusive(p)(x)`` is the linear-interpolation
      percentile matching DuckDB's ``QUANTILE_CONT`` / standard
      ``PERCENTILE_CONT`` semantics.
    - ``date_trunc`` returns ``Date`` (not ``DateTime``) for week and
      coarser grains, so the result is re-cast to the timestamp type to
      keep rows comparable with the DuckDB reference. Week truncation
      is ISO Monday (``date_trunc('week', '2026-06-11') = 2026-06-08``),
      matching DuckDB.
    - ``<=>`` (isNotDistinctFrom) is accepted ONLY inside JOIN ON
      sections — which is the only place the compiler emits
      ``null_safe_eq`` (temporal joins, entity-hop joins, snapshot
      joins). The portable ``IS NOT DISTINCT FROM`` is equally
      JOIN-ON-only on ClickHouse, so ``<=>`` is the native spelling.
    - ``DATE_DIFF('unit', start, end)`` needs explicitly nullable DateTime64
      endpoint casts to preserve NULLs and pre-1970 dates. Its Monday-start
      week boundaries differ from DuckDB, so
      authored week calls are refused by the scalar-call guard.
      ``DATE_ADD(date, INTERVAL n UNIT)`` uses the portable default.
    - Conditional aggregates keep the portable ``CASE`` default —
      ClickHouse ``COUNT``/``SUM`` skip NULLs correctly.
    """

    like_escape_clause = False
    # LOWER folds ASCII only, and lowerUTF8 refuses FixedString and Enum: toString reads any type
    # as String (NULL stays NULL), and only a FixedString loses its zero padding; a String keeps
    # every byte. The letters lowerUTF8 leaves as they are are refused on every warehouse.
    case_fold = (
        "lowerUTF8(if(position(toTypeName({0}), 'FixedString') > 0, "
        "toStringCutToZero(toString({0})), toString({0})))"
    )
    identifiers_case_sensitive = True
    null_boolean_type = "Nullable(Bool)"
    correlated_exists = False  # 24.8 has none
    label = "ClickHouse"
    call_names = PORTABLE_CALL_NAMES - {"TRIM"}
    date_diff_units = tuple(unit for unit in CONVERSION_WINDOW_UNITS if unit != "week")

    name: str = "clickhouse"

    def prepare_query(self, sql: str) -> PreparedQuery:
        # An unmatched outer-join field reads its type's default (0 or '') unless the join
        # yields NULLs, and a group a leaf has no row for must read NULL, not a made-up 0.
        return PreparedQuery(f"{sql}\nSETTINGS join_use_nulls = 1")

    def cast_type_name(self, logical_type: str) -> str:
        base = logical_type.split("(")[0]
        native = {"DOUBLE": "Float64", "INTEGER": "Int64", "BIGINT": "Int64", "VARCHAR": "String"}
        return f"Nullable({native.get(base, logical_type)})"

    def date_diff(self, unit: str, start_expr: Any, end_expr: Any) -> Any:
        # DateTime64 preserves pre-1970 dates; Nullable preserves NULL even with
        # cast_keep_nullable=0.
        return SqlCall(
            "DATE_DIFF",
            [
                SqlLiteral(unit),
                SqlCast(start_expr, "Nullable(DateTime64(6))"),
                SqlCast(end_expr, "Nullable(DateTime64(6))"),
            ],
        )

    # No day_series override: it was withheld because an unmatched LEFT JOIN field reads 0
    # rather than NULL. Every compiled statement now sets join_use_nulls, but a generated
    # series is untested here, so packages on ClickHouse keep needing an authored calendar
    # for dense fill.

    @property
    def outer_lookup_joins(self) -> bool:
        # The same default fill: an unmatched key or dimension reads '' or 0 unless its column
        # is Nullable, so a kept row would pair, group and filter as if it had a real value.
        return False

    def convert_timezone(self, source_tz: str, target_tz: str, ts_expr: Any) -> Any:
        # ClickHouse: toDateTime(ts, tz) reads the value in `tz`;
        # toTimeZone(dt, tz) re-renders it in the target zone.
        return SqlCall(
            "TO_TIME_ZONE",
            [SqlCall("TO_DATE_TIME", [ts_expr, SqlLiteral(source_tz)]), SqlLiteral(target_tz)],
        )

    def quote_string_literal(self, value: str) -> str:
        return backslash_escaped_string_literal(value)

    def percentile_cont(self, expr: Any, percentile: float) -> Any:
        # ClickHouse parameterized-aggregate syntax:
        # quantileExactInclusive(p)(x) — exact linear interpolation.
        from .sql_ast import SqlParameterizedCall

        return SqlParameterizedCall("QUANTILE_EXACT_INCLUSIVE", [SqlLiteral(percentile)], [expr])

    def median(self, expr: Any) -> Any:
        return self.percentile_cont(expr, 0.5)

    # argMin/argMax skip a NULL value and a NULL key alike, as arg_min does
    # (clickhouse.com/docs/sql-reference/aggregate-functions/reference/argmin).
    def first_value(self, expr: Any, order_expr: Any) -> Any:
        return SqlCall("ARGMIN", [expr, order_expr])  # renders argMin

    def last_value(self, expr: Any, order_expr: Any) -> Any:
        return SqlCall("ARGMAX", [expr, order_expr])  # renders argMax

    def date_trunc(self, grain: str, ts_expr: Any) -> Any:
        # ClickHouse date_trunc returns Date for 'week'/'month'/
        # 'quarter'/'year'; re-cast so every grain yields a timestamp
        # like DuckDB/Snowflake.
        return self.timestamp_cast(super().date_trunc(grain, ts_expr))

    def null_safe_eq(self, left: Any, right: Any) -> Any:
        return SqlBinary(left, "<=>", right)

    def window_lag(
        self,
        value_expr: Any,
        offset_rows: int,
        *,
        partition_by: list[Any],
        order_by: list[Any],
    ) -> Any:
        """LAG-equivalent window expression for prior_period.

        ClickHouse 24.x has no ``LAG`` window function (only
        ``lagInFrame``). Emulate exact LAG semantics — verified live
        against ``lagInFrame(toNullable(x), n, NULL)`` including
        partitions and NULL leading rows::

            CASE WHEN COUNT(1) OVER w = n + 1
                 THEN argMin(x, <order_key>) OVER w END
            -- w = (PARTITION BY …, ORDER BY key ASC,
            --      ROWS BETWEEN n PRECEDING AND CURRENT ROW)

        The frame holds n+1 rows exactly when n rows precede, and the
        frame's argMin by the order key is the row exactly n back.
        """
        from .sql_ast import SqlWindow

        offset = int(offset_rows)
        frame = f"ROWS BETWEEN {offset} PRECEDING AND CURRENT ROW"
        order_key = order_by[0].expr
        count_window = SqlWindow(
            function=SqlCall("COUNT", [SqlLiteral(1)]),
            partition_by=list(partition_by),
            order_by=list(order_by),
            frame=frame,
        )
        value_window = SqlWindow(
            function=SqlCall("ARGMIN", [value_expr, order_key]),
            partition_by=list(partition_by),
            order_by=list(order_by),
            frame=frame,
        )
        return SqlCase(
            whens=[
                SqlCaseWhen(
                    condition=SqlBinary(count_window, "=", SqlLiteral(offset + 1)),
                    result=value_window,
                )
            ],
            else_expr=None,
        )

    def capabilities(self) -> dict[str, Any]:
        payload = super().capabilities()
        payload.update(
            {
                # `<=>` / IS NOT DISTINCT FROM are JOIN-ON-only on
                # ClickHouse; the compiler only emits null_safe_eq in
                # join conditions, so this capability holds there.
                "null_safe_equality_operator": "<=>",
                "percentile_function": "quantileExactInclusive",
                # LAG is emulated (see window_lag) — ClickHouse 24.x
                # has no native LAG/LEAD window functions.
                "window_lag_native": False,
            }
        )
        return payload


# Redshift is intentionally NOT registered yet (account pending
# verification). When it lands it is exactly one dialect class + one
# adapter + one registry entry — see docs/ADDING_A_DIALECT.md, which
# uses Redshift as its worked example:
#
#   @dataclass(frozen=True)
#   class RedshiftDialect(SqlDialect):
#       name: str = "redshift"
#       # PG-flavored: PERCENTILE_CONT ... WITHIN GROUP, DATEDIFF/DATEADD,
#       # no IS NOT DISTINCT FROM pre-2023 clusters (use the portable
#       # CASE fallback when targeting those).
#
#   "redshift": WarehouseConnectorSpec(
#       name="redshift",
#       dialect=RedshiftDialect(),
#       connection_kinds=("redshift_native",),
#       connection_options=REDSHIFT_CONNECTION_OPTIONS,
#       adapter="semantic_rails.db_parts.redshift:create_adapter",
#   ),


@dataclass(frozen=True)
class WarehouseConnectorSpec:
    name: str
    dialect: SqlDialect
    connection_kinds: tuple[str, ...] = ()
    connection_options: tuple[str, ...] = ()
    requires_default_db: bool = False
    requires_seed: bool = False
    requires_connection_name: bool = False
    # "module:callable" entry point resolved lazily by
    # semantic_rails.db.create_warehouse_adapter. The callable receives
    # (package: PackageMeta, *, db_path: str) and returns a
    # WarehouseAdapter. Empty means the warehouse is dialect-only (it
    # can compile SQL but has no execution adapter).
    adapter: str = ""


SNOWFLAKE_CLI_CONNECTION_OPTIONS: tuple[str, ...] = ("database", "schema", "warehouse", "role")
SNOWFLAKE_NATIVE_CONNECTION_OPTIONS: tuple[str, ...] = (
    "account_env",
    "user_env",
    "password_env",
    "private_key_file",
    "private_key_env",
    "private_key_passphrase_env",
    "token_env",
    "token_file",
    "authenticator",
    "database",
    "schema",
    "warehouse",
    "role",
    "query_tag",
    "statement_timeout_seconds",
    "connect_timeout_seconds",
    "read_timeout_seconds",
)

SNOWFLAKE_NATIVE_DIRECT_AUTH_OPTIONS: tuple[str, ...] = (
    "password_env",
    "token_env",
    "token_file",
    "private_key_file",
    "private_key_env",
)

# Experimental ADBC path: password or PKCS #8 key-pair auth only.
SNOWFLAKE_ADBC_CONNECTION_OPTIONS: tuple[str, ...] = (
    "account",
    "account_env",
    "user",
    "user_env",
    "password_env",
    "password_file",
    "private_key_file",
    "private_key_env",
    "private_key_passphrase_env",
    "private_key_passphrase_file",
    "database",
    "schema",
    "warehouse",
    "role",
    "query_tag",
    "statement_timeout_seconds",
    "use_high_precision",
)

# Per-warehouse connection-option schemas. Secrets follow the package
# convention: never literals — env-var indirection (`*_env`) or file
# paths (`*_file`) only. Non-secret locators (host, port, database,
# schema, …) may be literals or env-indirected.
POSTGRES_CONNECTION_OPTIONS: tuple[str, ...] = (
    "host",
    "host_env",
    "port",
    "database",
    "schema",
    "user",
    "user_env",
    "password_env",
    "password_file",
    "sslmode",
    "statement_timeout_seconds",
    "connect_timeout_seconds",
    "read_timeout_seconds",
)

BIGQUERY_CONNECTION_OPTIONS: tuple[str, ...] = (
    "project",
    "project_env",
    "dataset",
    "location",
    "credentials_file",
    "credentials_file_env",
    "connect_timeout_seconds",
    "read_timeout_seconds",
)

DATABRICKS_CONNECTION_OPTIONS: tuple[str, ...] = (
    "host",
    "host_env",
    "http_path",
    "http_path_env",
    "token_env",
    "token_file",
    "catalog",
    "schema",
    "connect_timeout_seconds",
    "read_timeout_seconds",
)

MOTHERDUCK_CONNECTION_OPTIONS: tuple[str, ...] = (
    "database",
    "schema",
    "token_env",
    "token_file",
)

DUCKLAKE_CONNECTION_OPTIONS: tuple[str, ...] = (
    "catalog_path",
    "catalog_path_env",
    "data_path",
    "data_path_env",
    "schema",
)

ATHENA_CONNECTION_OPTIONS: tuple[str, ...] = (
    "region",
    "region_env",
    "database",
    "workgroup",
    "s3_staging_dir",
    "s3_staging_dir_env",
    "connect_timeout_seconds",
    "read_timeout_seconds",
)

CLICKHOUSE_CONNECTION_OPTIONS: tuple[str, ...] = (
    "host",
    "host_env",
    "port",
    "database",
    "user",
    "user_env",
    "password_env",
    "password_file",
    "secure",
    "connect_timeout_seconds",
    "read_timeout_seconds",
)


_WAREHOUSE_CONNECTORS: dict[str, WarehouseConnectorSpec] = {
    "duckdb": WarehouseConnectorSpec(
        name="duckdb",
        dialect=DuckDbDialect(),
        requires_default_db=True,
        requires_seed=True,
        adapter="semantic_rails.db:create_duckdb_adapter",
    ),
    "snowflake": WarehouseConnectorSpec(
        name="snowflake",
        dialect=SnowflakeDialect(),
        connection_kinds=("snowflake_cli", "snowflake_native", "snowflake_adbc"),
        connection_options=tuple(
            dict.fromkeys(
                [
                    *SNOWFLAKE_CLI_CONNECTION_OPTIONS,
                    *SNOWFLAKE_NATIVE_CONNECTION_OPTIONS,
                    *SNOWFLAKE_ADBC_CONNECTION_OPTIONS,
                ]
            )
        ),
        requires_connection_name=True,
        adapter="semantic_rails.db_parts.snowflake:create_adapter",
    ),
    "postgres": WarehouseConnectorSpec(
        name="postgres",
        dialect=PostgresDialect(),
        connection_kinds=("postgres_native",),
        connection_options=POSTGRES_CONNECTION_OPTIONS,
        adapter="semantic_rails.db_parts.postgres:create_adapter",
    ),
    "bigquery": WarehouseConnectorSpec(
        name="bigquery",
        dialect=BigQueryDialect(),
        connection_kinds=("bigquery_native",),
        connection_options=BIGQUERY_CONNECTION_OPTIONS,
        adapter="semantic_rails.db_parts.bigquery:create_adapter",
    ),
    "databricks": WarehouseConnectorSpec(
        name="databricks",
        dialect=DatabricksDialect(),
        connection_kinds=("databricks_native",),
        connection_options=DATABRICKS_CONNECTION_OPTIONS,
        adapter="semantic_rails.db_parts.databricks:create_adapter",
    ),
    "motherduck": WarehouseConnectorSpec(
        name="motherduck",
        dialect=MotherDuckDialect(),
        connection_kinds=("motherduck_native",),
        connection_options=MOTHERDUCK_CONNECTION_OPTIONS,
        adapter="semantic_rails.db_parts.motherduck:create_adapter",
    ),
    "ducklake": WarehouseConnectorSpec(
        name="ducklake",
        dialect=DuckLakeDialect(),
        connection_kinds=("ducklake_native",),
        connection_options=DUCKLAKE_CONNECTION_OPTIONS,
        adapter="semantic_rails.db_parts.ducklake:create_adapter",
    ),
    "athena": WarehouseConnectorSpec(
        name="athena",
        dialect=AthenaDialect(),
        connection_kinds=("athena_native",),
        connection_options=ATHENA_CONNECTION_OPTIONS,
        adapter="semantic_rails.db_parts.athena:create_adapter",
    ),
    "clickhouse": WarehouseConnectorSpec(
        name="clickhouse",
        dialect=ClickHouseDialect(),
        connection_kinds=("clickhouse_native",),
        connection_options=CLICKHOUSE_CONNECTION_OPTIONS,
        adapter="semantic_rails.db_parts.clickhouse:create_adapter",
    ),
}


def supported_warehouses() -> tuple[str, ...]:
    return tuple(sorted(_WAREHOUSE_CONNECTORS))


def warehouse_connector(warehouse: str) -> WarehouseConnectorSpec | None:
    return _WAREHOUSE_CONNECTORS.get(str(warehouse or "duckdb").strip().lower() or "duckdb")


def normalize_connection_option_name(name: str) -> str:
    return str(name or "").strip().lower().replace("-", "_")


def connection_option_errors(warehouse: str, kind: str, options: dict[str, Any]) -> tuple[str, ...]:
    connector = warehouse_connector(warehouse)
    if connector is None:
        return ()
    if warehouse_connector(warehouse) and kind == "snowflake_cli":
        option_keys = SNOWFLAKE_CLI_CONNECTION_OPTIONS
    elif warehouse_connector(warehouse) and kind == "snowflake_native":
        option_keys = SNOWFLAKE_NATIVE_CONNECTION_OPTIONS
    elif warehouse_connector(warehouse) and kind == "snowflake_adbc":
        option_keys = SNOWFLAKE_ADBC_CONNECTION_OPTIONS
    else:
        option_keys = tuple(
            connector.connection_options if kind in connector.connection_kinds else ()
        )
    allowed = set(option_keys)
    allowed_text = ", ".join(option_keys) or "none"
    errors = []
    for raw_key, value in sorted((options or {}).items(), key=lambda item: str(item[0])):
        key = normalize_connection_option_name(str(raw_key))
        if key not in allowed:
            errors.append(
                f"unsupported package.connection option '{raw_key}' for {kind or 'unspecified'} (supported: {allowed_text})"
            )
            continue
        if not isinstance(value, str) or not value.strip():
            errors.append(f"package.connection option '{raw_key}' must be a non-empty string")
    return tuple(errors)


def snowflake_adbc_connect_errors(options: dict[str, Any]) -> tuple[str, ...]:
    """Check authored credential sources without reading environment variables or files."""
    keys = {normalize_connection_option_name(str(key)) for key in options}
    errors = [
        f"snowflake_adbc requires {key} or {key}_env"
        for key in ("account", "user")
        if not keys & {key, f"{key}_env"}
    ]
    has_password = bool(keys & {"password_env", "password_file"})
    has_key = bool(keys & {"private_key_env", "private_key_file"})
    if has_password == has_key:
        errors.append("snowflake_adbc requires exactly one password or PKCS #8 key source")
    for key in ("private_key_passphrase_env", "private_key_passphrase_file"):
        if key in keys and not has_key:
            errors.append(f"snowflake_adbc {key} requires a key source")
    return tuple(errors)


def snowflake_native_direct_connect_errors(options: dict[str, Any]) -> tuple[str, ...]:
    normalized = {
        normalize_connection_option_name(str(key)): value
        for key, value in dict(options or {}).items()
    }
    missing = []
    for key in ("account_env", "user_env"):
        value = normalized.get(key)
        if not isinstance(value, str) or not value.strip():
            missing.append(key)
    has_auth_option = any(
        isinstance(normalized.get(key), str) and str(normalized.get(key)).strip()
        for key in SNOWFLAKE_NATIVE_DIRECT_AUTH_OPTIONS
    )
    authenticator = str(normalized.get("authenticator", "") or "").strip().lower()
    if authenticator == "externalbrowser":
        has_auth_option = True
    if not has_auth_option:
        missing.append(
            "one of password_env, token_env, token_file, private_key_file, private_key_env, or authenticator=externalbrowser"
        )
    if missing:
        return (
            "snowflake_native direct connection without package.connection.name requires "
            + ", ".join(missing),
        )
    return ()


def dialect_for_warehouse(warehouse: str) -> SqlDialect:
    connector = warehouse_connector(warehouse)
    if connector is not None:
        return connector.dialect
    return SqlDialect(name=str(warehouse or "generic"))
