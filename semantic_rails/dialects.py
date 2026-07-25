"""SQL dialect adapters — DuckDB, Snowflake, and warehouse capabilities.

Defines :class:`SqlDialect` and concrete subclasses for the supported
warehouses, plus the :func:`dialect_for_warehouse` factory the renderer
and compiler call. Also owns the connection-option schema used by
``parse-config`` to validate warehouse blocks (``supported_warehouses``,
``connection_option_errors``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCast,
    SqlDatePart,
    SqlInterval,
    SqlLiteral,
    SqlOrderTerm,
    SqlWithinGroup,
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

    Snowflake, BigQuery, ClickHouse and Databricks/Spark all treat a
    backslash as introducing an escape sequence inside a single-quoted
    literal. Doubling only the quote is therefore not enough: a value
    ending in a backslash escapes the *closing* quote, so everything the
    caller supplied after it is parsed as SQL rather than as data.

    Backslashes are doubled before quotes so the doubling cannot be
    applied to a quote-escape this function introduced itself.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


@dataclass(frozen=True)
class SqlDialect:
    name: str

    def quote_string_literal(self, value: str) -> str:
        """Render ``value`` as a single-quoted SQL string literal.

        Standard-conforming default: the single quote is the only
        character with meaning inside a literal, and it is escaped by
        doubling it. This is correct for DuckDB, MotherDuck, DuckLake,
        Postgres (``standard_conforming_strings=on``, the default since
        9.1) and Trino/Athena. Dialects that also honour backslash
        escapes override this with
        :func:`backslash_escaped_string_literal` — doubling backslashes
        *here* would corrupt the value on these warehouses instead.
        """
        return "'" + value.replace("'", "''") + "'"

    def percentile_cont(self, expr: Any, percentile: float) -> Any:
        return SqlCall("QUANTILE_CONT", [expr, SqlLiteral(percentile)])

    def median(self, expr: Any) -> Any:
        return SqlCall("MEDIAN", [expr])

    def first_value(self, expr: Any, order_expr: Any) -> Any:
        return SqlCall("arg_min", [expr, order_expr])

    def last_value(self, expr: Any, order_expr: Any) -> Any:
        return SqlCall("arg_max", [expr, order_expr])

    def timestamp_type_name(self) -> str:
        return "TIMESTAMP"

    def timestamp_cast(self, expr: Any) -> Any:
        return SqlCast(expr, self.timestamp_type_name())

    def date_trunc(self, grain: str, ts_expr: Any) -> Any:
        return SqlCall("DATE_TRUNC", [SqlLiteral(grain), self.timestamp_cast(ts_expr)])

    def convert_timezone(self, source_tz: str, target_tz: str, ts_expr: Any) -> Any:
        # Generic fallback: CONVERT_TIMEZONE(source, target, expr) — Snowflake
        # syntax; subclasses override for other dialects.
        return SqlCall(
            "CONVERT_TIMEZONE",
            [SqlLiteral(source_tz), SqlLiteral(target_tz), ts_expr],
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


@dataclass(frozen=True)
class SnowflakeDialect(SqlDialect):
    name: str = "snowflake"

    def quote_string_literal(self, value: str) -> str:
        return backslash_escaped_string_literal(value)

    def percentile_cont(self, expr: Any, percentile: float) -> Any:
        return SqlWithinGroup(
            SqlCall("PERCENTILE_CONT", [SqlLiteral(percentile)]),
            order_by=[SqlOrderTerm(expr=expr, direction="ASC")],
        )

    def first_value(self, expr: Any, order_expr: Any) -> Any:
        return SqlCall("MIN_BY", [expr, order_expr])

    def last_value(self, expr: Any, order_expr: Any) -> Any:
        return SqlCall("MAX_BY", [expr, order_expr])

    def timestamp_type_name(self) -> str:
        return "TIMESTAMP_NTZ"

    def date_diff(self, unit: str, start_expr: Any, end_expr: Any) -> Any:
        return SqlCall(
            "DATEDIFF",
            [SqlLiteral(unit), self.timestamp_cast(start_expr), self.timestamp_cast(end_expr)],
        )

    def date_add(self, unit: str, value_expr: Any, date_expr: Any) -> Any:
        return SqlCall("DATEADD", [SqlDatePart(unit), value_expr, date_expr])

    def null_safe_eq(self, left: Any, right: Any) -> Any:
        return SqlCall("EQUAL_NULL", [left, right])

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
class DuckDbDialect(SqlDialect):
    name: str = "duckdb"


@dataclass(frozen=True)
class PostgresDialect(SqlDialect):
    """PostgreSQL (psycopg 3, ``postgres_native``).

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

    name: str = "postgres"

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

    def percentile_cont(self, expr: Any, percentile: float) -> Any:
        return SqlWithinGroup(
            SqlCall("PERCENTILE_CONT", [SqlLiteral(percentile)]),
            order_by=[SqlOrderTerm(expr=expr, direction="ASC")],
        )

    def median(self, expr: Any) -> Any:
        # PG has no MEDIAN; PERCENTILE_CONT(0.5) is the exact equivalent.
        return self.percentile_cont(expr, 0.5)

    def _value_by(self, expr: Any, order_expr: Any, direction: str) -> Any:
        from .sql_ast import SqlCallWithOrder, SqlIndexAccess

        return SqlIndexAccess(
            SqlCallWithOrder(
                SqlCall("ARRAY_AGG", [expr]),
                order_by=[SqlOrderTerm(expr=order_expr, direction=direction)],
            ),
            SqlLiteral(1),
        )

    def first_value(self, expr: Any, order_expr: Any) -> Any:
        return self._value_by(expr, order_expr, "ASC")

    def last_value(self, expr: Any, order_expr: Any) -> Any:
        return self._value_by(expr, order_expr, "DESC")

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

    name: str = "bigquery"

    def quote_string_literal(self, value: str) -> str:
        return backslash_escaped_string_literal(value)

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

        via ``(ARRAY_AGG(expr ORDER BY expr ASC))[OFFSET(...)]`` array
        references. When ``h`` is integral FLOOR = CEIL and the fraction
        term is 0, so the result is the element itself. NOTE: BigQuery
        ARRAY_AGG errors on NULL elements (IGNORE NULLS is not
        representable in the SQL AST) — see capabilities().
        """
        from .sql_ast import SqlCallWithOrder, SqlIndexAccess

        def sorted_values() -> Any:
            return SqlCallWithOrder(
                call=SqlCall("ARRAY_AGG", [expr]),
                order_by=[SqlOrderTerm(expr=expr, direction="ASC")],
            )

        def position() -> Any:
            # h = p * (COUNT(expr) - 1); COUNT(expr) matches ARRAY_AGG's
            # element count (both skip nothing — NULLs error out above).
            return SqlBinary(
                SqlLiteral(float(percentile)),
                "*",
                SqlBinary(SqlCall("COUNT", [expr]), "-", SqlLiteral(1)),
            )

        def element_at(index_call: str) -> Any:
            return SqlIndexAccess(
                expr=sorted_values(),
                index=SqlCast(SqlCall(index_call, [position()]), "BIGINT"),
                wrapper="OFFSET",
            )

        lower = element_at("FLOOR")
        upper = element_at("CEIL")
        fraction = SqlBinary(position(), "-", SqlCall("FLOOR", [position()]))
        return SqlBinary(lower, "+", SqlBinary(fraction, "*", SqlBinary(upper, "-", lower)))

    def median(self, expr: Any) -> Any:
        return self.percentile_cont(expr, 0.5)

    def _array_agg_edge(self, expr: Any, order_expr: Any, direction: str) -> Any:
        # No MIN_BY/MAX_BY — use ARRAY_AGG(expr ORDER BY o … LIMIT 1)
        # [OFFSET(0)]. NOTE: BigQuery ARRAY_AGG errors on NULL elements
        # and IGNORE NULLS is not representable in the SQL AST (see
        # capabilities()).
        from .sql_ast import SqlCallWithOrder, SqlIndexAccess

        return SqlIndexAccess(
            expr=SqlCallWithOrder(
                call=SqlCall("ARRAY_AGG", [expr]),
                order_by=[SqlOrderTerm(expr=order_expr, direction=direction)],
                limit=1,
            ),
            index=SqlLiteral(0),
            wrapper="OFFSET",
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
        # CAST(x AS DATETIME) is not representable today (DATETIME is
        # not in sql_ast.SQL_CAST_TYPE_NAMES), so rely on BigQuery's
        # implicit coercions instead: DATE -> DATETIME and string
        # literal -> DATETIME both coerce wherever the DATETIME_*
        # functions and comparisons expect a DATETIME.
        return expr

    def date_trunc(self, grain: str, ts_expr: Any) -> Any:
        # BigQuery reverses the portable order: DATETIME_TRUNC(ts, unit)
        # with the unit as a keyword. 'week' maps to ISOWEEK (Monday
        # start) for parity with DuckDB's ISO-8601 date_trunc('week').
        part = "isoweek" if str(grain).strip().lower() == "week" else str(grain)
        return SqlCall("DATETIME_TRUNC", [self.timestamp_cast(ts_expr), SqlDatePart(part)])

    def date_diff(self, unit: str, start_expr: Any, end_expr: Any) -> Any:
        # BigQuery reverses the portable order: DATETIME_DIFF(end,
        # start, unit) — END FIRST. Boundary-crossing semantics match
        # DuckDB's date_diff.
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
                    "exact linear interpolation over "
                    "(ARRAY_AGG(expr ORDER BY expr))[OFFSET(FLOOR/CEIL(p * (COUNT(expr) - 1)))] "
                    "— PERCENTILE_CONT is window-only in BigQuery, so the exact "
                    "DuckDB QUANTILE_CONT definition is rebuilt from aggregates. "
                    "CAVEAT: errors if aggregated values contain NULL elements "
                    "(ARRAY_AGG rejects NULLs; IGNORE NULLS is not representable "
                    "in the SQL AST)."
                ),
                "median_exact": True,
                "first_last_value_method": (
                    "(ARRAY_AGG(expr ORDER BY order LIMIT 1))[OFFSET(0)] — "
                    "errors if aggregated values contain NULL elements; "
                    "IGNORE NULLS is not representable in the SQL AST."
                ),
            }
        )
        return payload


@dataclass(frozen=True)
class DatabricksDialect(SqlDialect):
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

    def quote_string_literal(self, value: str) -> str:
        return backslash_escaped_string_literal(value)

    def percentile_cont(self, expr: Any, percentile: float) -> Any:
        return SqlWithinGroup(
            SqlCall("PERCENTILE_CONT", [SqlLiteral(percentile)]),
            order_by=[SqlOrderTerm(expr=expr, direction="ASC")],
        )

    def first_value(self, expr: Any, order_expr: Any) -> Any:
        return SqlCall("MIN_BY", [expr, order_expr])

    def last_value(self, expr: Any, order_expr: Any) -> Any:
        return SqlCall("MAX_BY", [expr, order_expr])

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
    (``DATE_TRUNC('month', CAST(.. AS TIMESTAMP))``), ``date_diff``
    (``DATE_DIFF('unit', start, end)`` with a lowercase unit string),
    null-safe equality (``IS NOT DISTINCT FROM``), and the conditional
    aggregate CASE form. Overrides cover ``DATE_ADD('unit', n, ts)``
    (Trino rejects the INTERVAL form), ``MIN_BY``/``MAX_BY``, and the
    percentile family — Trino has NO exact ``PERCENTILE_CONT``
    aggregate and ``APPROX_PERCENTILE`` is a t-digest sketch, so
    percentiles lower to an EXACT linear interpolation over a sorted
    ``ARRAY_AGG`` (see :meth:`percentile_cont`).
    """

    name: str = "athena"

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

    def first_value(self, expr: Any, order_expr: Any) -> Any:
        return SqlCall("MIN_BY", [expr, order_expr])

    def last_value(self, expr: Any, order_expr: Any) -> Any:
        return SqlCall("MAX_BY", [expr, order_expr])

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
    - ``DATE_DIFF('unit', start, end)`` / ``DATE_ADD(date, INTERVAL n
      UNIT)`` aliases work as-is with boundary-crossing semantics and
      Monday-start weeks (DuckDB parity), so the portable defaults
      stand.
    - Conditional aggregates keep the portable ``CASE`` default —
      ClickHouse ``COUNT``/``SUM`` skip NULLs correctly.
    """

    name: str = "clickhouse"

    def quote_string_literal(self, value: str) -> str:
        return backslash_escaped_string_literal(value)

    def percentile_cont(self, expr: Any, percentile: float) -> Any:
        # ClickHouse parameterized-aggregate syntax:
        # quantileExactInclusive(p)(x) — exact linear interpolation.
        from .sql_ast import SqlParameterizedCall

        return SqlParameterizedCall("QUANTILE_EXACT_INCLUSIVE", [SqlLiteral(percentile)], [expr])

    def median(self, expr: Any) -> Any:
        return self.percentile_cont(expr, 0.5)

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
)

SNOWFLAKE_NATIVE_DIRECT_AUTH_OPTIONS: tuple[str, ...] = (
    "password_env",
    "token_env",
    "token_file",
    "private_key_file",
    "private_key_env",
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
)

BIGQUERY_CONNECTION_OPTIONS: tuple[str, ...] = (
    "project",
    "project_env",
    "dataset",
    "location",
    "credentials_file",
    "credentials_file_env",
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
        connection_kinds=("snowflake_cli", "snowflake_native"),
        connection_options=tuple(
            dict.fromkeys([*SNOWFLAKE_CLI_CONNECTION_OPTIONS, *SNOWFLAKE_NATIVE_CONNECTION_OPTIONS])
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
