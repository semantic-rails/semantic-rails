"""The time series a query returns: the implicit calendar's day spine, a calendar fill's
binding, dense periods between the first and the last, and the loaded coverage plan.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from ..errors import SemanticLayerError
from ..expressions import (
    ConversionExpr,
    MetricPredicateExpr,
    expr_to_dict,
)
from ..ir import (
    LogicalPlan,
)
from ..schema import (
    PackageConfig,
)
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCast,
    SqlCte,
    SqlField,
    SqlIdentifier,
    SqlJoin,
    SqlLiteral,
    SqlSelect,
    SqlTableRef,
)
from .aliasing import AliasRegistry, alias_select
from .bind import (
    _collect_conversion_exprs,
    _freeze_payload,
    _parse_public_expr,
)
from .lowering_common import _calendar_bound, _dialect, _emits_time_coverage, _whole_day_window
from .paths import (
    _column_ref,
)


def coverage_base_plan(
    plan: LogicalPlan, config: PackageConfig, strategies: Sequence[str]
) -> LogicalPlan:
    """A plan that emits base coverage reads the base, with each leaf's pre-routing strategy."""
    if not _emits_time_coverage(plan, config):
        return plan
    return replace(
        plan,
        measure_plans=[
            replace(
                row,
                aggregate_relation_id="",
                rewrite_strategy=strategy,
                aggregate_relation_rejections={
                    **{
                        rel.id: "base_time_coverage_required"
                        for rel in config.aggregate_relations
                        if rel.source_entity == row.source_entity
                    },
                    **row.aggregate_relation_rejections,
                },
            )
            for row, strategy in zip(plan.measure_plans, strategies, strict=True)
        ],
    )


def _window_is_named(time: Mapping[str, Any]) -> bool:
    """A named window (``last``, ``this``, or ``start`` and ``end``) answers every period."""
    return bool(
        time.get("last_unit")
        or time.get("this")
        or (
            time.get("start") is not None and time.get("end") is not None and not time.get("to_now")
        )
    )


def _calendar_fill_binding(
    plan: LogicalPlan, config: PackageConfig, *, force: bool = False
) -> tuple[str, str, str | None] | None:
    """A filled series hangs off the implicit Gregorian calendar, bucketed like the leaf.

    No authored calendar table takes part: its period columns and ``date_day`` are
    never read for bucketing (validation refuses every non-default calendar).
    """
    if not plan.time or (not plan.time.get("fill") and not force):
        return None
    grain = str(plan.time.get("grain", "") or "").lower()
    if not grain:
        raise SemanticLayerError("INVALID_QUERY", "time.fill needs time.grain")
    if grain not in {"day", "week", "month", "quarter", "year"}:
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED", f"time.fill does not support grain '{grain}'"
        )
    # A window with only a start runs to now but stays open: its series spans its own periods.
    bounded = (
        plan.time.get("start") is not None
        and plan.time.get("end") is not None
        and not plan.time.get("to_now")
    )
    return _IMPLICIT_CALENDAR, "bucket", "date_day" if bounded else None


_IMPLICIT_CALENDAR = "implicit_calendar"


def _implicit_calendar_ctes(plan: LogicalPlan, config: PackageConfig) -> list[SqlCte]:
    """The implicit Gregorian calendar: a day spine bucketed like the leaf.

    The days run over ``dense_bounds``: the leaf's own first and last buckets, or,
    for a ``start``/``end`` window, the bounds' dates widened by three days (a
    UTC-offset bound can fall two days away in the role's zone), which the exact
    ``_whole_day_window`` predicate then narrows. Each day's bucket is the leaf's
    own ``date_trunc``, so labels, week starts and types match the leaf's buckets.
    """
    dialect = _dialect(config)
    ctes: list[SqlCte] = []
    if (
        plan.time.get("start") is not None
        and plan.time.get("end") is not None
        and not plan.time.get("to_now")
    ):
        ordinals = []
        for key in ("start", "end"):
            moment = _calendar_bound(plan.time[key])
            if moment is None:
                raise SemanticLayerError(
                    "REWRITE_NOT_SUPPORTED",
                    f"time.{key} must be an ISO date or timestamp for the implicit calendar",
                    details={key: plan.time[key]},
                )
            ordinals.append(moment.toordinal())
        first = max(ordinals[0] - 3, 1)
        last = max(min(ordinals[1] + 3, datetime.max.toordinal()), first)
        ctes.append(
            SqlCte(
                name="dense_bounds",
                query=SqlSelect(
                    select=[
                        SqlField(
                            SqlCast(
                                SqlLiteral(datetime.fromordinal(day).date().isoformat()), "DATE"
                            ),
                            alias,
                        )
                        for day, alias in ((first, "range_start"), (last, "range_end"))
                    ]
                ),
            )
        )
    start, end = (
        SqlCast(SqlIdentifier(parts=["dense_bounds", column]), "DATE")
        for column in ("range_start", "range_end")
    )
    ctes.append(SqlCte(name="implicit_days", query=dialect.day_series(start, end, "dense_bounds")))
    day = SqlIdentifier(parts=["implicit_days", "date_day"])
    bucket = dialect.date_trunc(str(plan.time["grain"]).lower(), day)
    ctes.append(
        SqlCte(
            name=_IMPLICIT_CALENDAR,
            query=SqlSelect(
                select=[SqlField(day, "date_day"), SqlField(bucket, "bucket")],
                from_table=SqlTableRef(name="implicit_days"),
            ),
        )
    )
    return ctes


def _source_bucket_recovery_ctes(time_alias: str) -> list[SqlCte]:
    """Keep buckets selected by the source when offset typing is unspecified."""
    calendar_bucket = SqlIdentifier(parts=["calendar_time", time_alias])
    leaf_bucket = SqlIdentifier(parts=["leaf_time_keys", time_alias])
    # An unmatched ClickHouse outer-join field defaults to zero rather than NULL.
    # A constant marker identifies real source rows on both join behaviors.
    time_bucket = SqlCase(
        whens=[
            SqlCaseWhen(
                condition=SqlBinary(
                    SqlIdentifier(parts=["leaf_time_keys", "source_present"]), "=", SqlLiteral(1)
                ),
                result=leaf_bucket,
            )
        ],
        else_expr=calendar_bucket,
    )
    keys = SqlCte(
        name="leaf_time_keys",
        query=SqlSelect(
            select=[
                SqlField(SqlIdentifier(parts=["leaf_base", time_alias]), time_alias),
                SqlField(SqlLiteral(1), "source_present"),
            ],
            from_table=SqlTableRef(name="leaf_base"),
            # A NULL time has no place in a series (the calendar never holds it).
            where=[
                SqlBinary(
                    SqlIdentifier(parts=["leaf_base", time_alias]), "IS NOT", SqlLiteral(None)
                )
            ],
            group_by=[SqlIdentifier(parts=["leaf_base", time_alias])],
        ),
    )
    dense = SqlCte(
        name="dense_time",
        query=SqlSelect(
            select=[SqlField(time_bucket, time_alias)],
            from_table=SqlTableRef(name="calendar_time"),
            joins=[
                SqlJoin(
                    join_type="FULL OUTER",
                    table=SqlTableRef(name="leaf_time_keys"),
                    on=SqlBinary(calendar_bucket, "=", leaf_bucket),
                )
            ],
            group_by=[time_bucket],
        ),
    )
    return [keys, dense]


def _dense_time_ctes(
    plan: LogicalPlan, config: PackageConfig, binding: tuple[str, str, str | None], time_alias: str
) -> list[SqlCte]:
    """The ``dense_time`` series of time buckets a filled query's rows hang off."""
    ctes: list[SqlCte] = []
    calendar_table, calendar_column, day = binding
    calendar_expr = _column_ref(calendar_table, calendar_column)
    dense_time_joins: list[SqlJoin] = []
    if day is not None:
        dense_time_where = _whole_day_window(_column_ref(calendar_table, day), plan.time, config)
    else:
        ctes.append(
            SqlCte(
                name="dense_bounds",
                query=SqlSelect(
                    select=[
                        SqlField(
                            SqlCall("MIN", [SqlIdentifier(parts=["leaf_base", time_alias])]),
                            "range_start",
                        ),
                        SqlField(
                            SqlCall("MAX", [SqlIdentifier(parts=["leaf_base", time_alias])]),
                            "range_end",
                        ),
                    ],
                    from_table=SqlTableRef(name="leaf_base"),
                ),
            )
        )
        # CROSS JOIN with a 1-row scalar source reads more honestly than
        # `INNER JOIN ... ON TRUE`, which looks like a bailout.
        dense_time_joins.append(
            SqlJoin(join_type="CROSS", table=SqlTableRef(name="dense_bounds"), on=None)
        )
        dense_time_where = [
            SqlBinary(calendar_expr, ">=", SqlIdentifier(parts=["dense_bounds", "range_start"])),
            SqlBinary(calendar_expr, "<=", SqlIdentifier(parts=["dense_bounds", "range_end"])),
        ]
    ctes.extend(_implicit_calendar_ctes(plan, config))
    # The spine keeps every source bucket too, so a generated series that falls short
    # can only lose an empty bucket, never data.
    ctes.append(
        SqlCte(
            name="calendar_time",
            query=SqlSelect(
                select=[SqlField(calendar_expr, time_alias)],
                from_table=SqlTableRef(name=calendar_table),
                joins=dense_time_joins,
                where=dense_time_where,
                group_by=[calendar_expr],
            ),
        )
    )
    ctes.extend(_source_bucket_recovery_ctes(time_alias))
    return ctes


def _series_base_ctes(
    plan: LogicalPlan,
    config: PackageConfig,
    binding: tuple[str, str, str | None] | None,
    time_alias: str,
    group_aliases: list[str],
    value_aliases: list[str],
) -> list[SqlCte]:
    """``series_base``: every period of ``dense_time`` for each group ``leaf_base`` holds, with
    ``leaf_base``'s values where it has the row (NULL elsewhere, for the guard to settle).

    ``binding`` is the calendar the periods come from; without one the series is the window
    total's single bucket.
    """
    if binding is not None:
        ctes = _dense_time_ctes(plan, config, binding, time_alias)
    else:
        spine = SqlSelect(select=[SqlField(SqlLiteral(0), time_alias)])
        ctes = [SqlCte(name="dense_time", query=spine)]
    joins: list[SqlJoin] = []
    filled_fields: list[SqlField] = []
    from_table = SqlTableRef(name="dense_time")
    if group_aliases:
        ctes.append(
            SqlCte(
                name="dense_groups",
                query=SqlSelect(
                    select=[
                        SqlField(SqlIdentifier(parts=["leaf_base", alias]), alias)
                        for alias in group_aliases
                    ],
                    from_table=SqlTableRef(name="leaf_base"),
                    group_by=[SqlIdentifier(parts=["leaf_base", alias]) for alias in group_aliases],
                ),
            )
        )
        from_table = SqlTableRef(name="dense_groups")
        # `dense_time` and `dense_groups` are independent enumerations; emit
        # the cross-product as CROSS JOIN rather than `INNER JOIN ... ON TRUE`.
        joins.append(SqlJoin(join_type="CROSS", table=SqlTableRef(name="dense_time"), on=None))
        filled_fields.extend(
            SqlField(SqlIdentifier(parts=["dense_groups", alias]), alias) for alias in group_aliases
        )
    filled_fields.append(SqlField(SqlIdentifier(parts=["dense_time", time_alias]), time_alias))
    join_condition: Any = SqlBinary(
        SqlIdentifier(parts=["dense_time", time_alias]),
        "=",
        SqlIdentifier(parts=["leaf_base", time_alias]),
    )
    for alias in group_aliases:
        join_condition = SqlBinary(
            join_condition,
            "AND",
            _dialect(config).null_safe_eq(
                SqlIdentifier(parts=["dense_groups", alias]),
                SqlIdentifier(parts=["leaf_base", alias]),
            ),
        )
    joins.append(SqlJoin(join_type="LEFT", table=SqlTableRef(name="leaf_base"), on=join_condition))
    filled_fields.extend(
        SqlField(SqlIdentifier(parts=["leaf_base", alias]), alias) for alias in value_aliases
    )
    # No inner ORDER BY: the outer SELECT controls final ordering.
    series = SqlSelect(select=filled_fields, from_table=from_table, joins=joins)
    return [*ctes, SqlCte(name="series_base", query=series)]


def _tier_internal_aliases(
    plan: LogicalPlan,
    select: SqlSelect,
    *,
    measure_aliases: Iterable[str],
    metric_filter_aliases: Iterable[str] = (),
) -> SqlSelect:
    registry = AliasRegistry.for_plan(
        plan, measure_aliases=measure_aliases, metric_filter_aliases=metric_filter_aliases
    )
    if not registry.aliases:
        return select
    return alias_select(select, registry, preserve_output_aliases=True)


def _conversion_exprs_for_plan(plan: LogicalPlan, config: PackageConfig) -> list[ConversionExpr]:
    conversions: dict[str, ConversionExpr] = {}
    for expr_payload in plan.post_aggregation_exprs.values():
        out: list[ConversionExpr] = []
        _collect_conversion_exprs(_parse_public_expr(expr_payload), config, out)
        for expr in out:
            conversions[_freeze_payload(expr_to_dict(expr))] = expr
    for item in list(plan.query.get("metric_filters", []) or []):
        raw_expr = dict(item.get("expression", {}) or {})
        if not raw_expr:
            continue
        parsed = _parse_public_expr(raw_expr)
        if isinstance(parsed, MetricPredicateExpr):
            continue
        _collect_conversion_exprs(parsed, config, out := [])
        for expr in out:
            conversions[_freeze_payload(expr_to_dict(expr))] = expr
    return list(conversions.values())
