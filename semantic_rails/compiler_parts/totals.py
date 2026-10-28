"""A total: its input at a coarser grain beside each row (``{"kind": "total"}``).

The coarser grain is the call itself with the total's ``over`` columns removed: the same
``where``, time window, entity filters, routes and package rules, compiled as a nested query
and joined back on the columns that remain. Every measure kind is computed from its own rows
at that grain, so a distinct count or an average is never added up from the rows. ``having``,
``order_by`` and ``limit`` act on the call's rows after the total, as for a share.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..ast import TIME_AXIS, time_output_alias
from ..errors import SemanticLayerError
from ..ir import LogicalPlan
from ..schema import PackageConfig
from ..sql_ast import (
    SqlCte,
    SqlExpr,
    SqlIdentifier,
    SqlJoin,
    SqlTableRef,
)
from . import nested
from .empty_groups import recording_outputs_as
from .lowering_common import _dialect
from .namespacing import _namespace_sql_select
from .paths import _join_condition, display_key

# The keys of a call the coarser call keeps as they are; the rest it sets or leaves out.
_CARRIED = ("version", "where", "temporal_role_overrides", "route_decisions", "observation_scope")
# A time block's keys that only a grain reads (and the planner's own window-total mark).
_GRAIN_KEYS = frozenset({"grain", "fill", "window_total"})


def selected_totals(select: Iterable[Mapping[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Each top-level select item that is a total, as ``(alias, expression)``."""
    return [
        (str(row.get("as")), dict(row["expression"]))
        for row in select
        if isinstance(row.get("expression"), Mapping) and row["expression"].get("kind") == "total"
    ]


def coarse_group_by(
    group_by: Iterable[str], over: Iterable[str], config: PackageConfig
) -> list[str]:
    """The group_by of the coarser call: without the ``over`` columns, nor the entity key a
    rolled-up name groups by beside it. A name the coarser call keeps groups by its entity's key
    beside it, as any call's does, so namesakes never merge: that key stays, ``over`` or not."""
    group_by, over = list(group_by), set(over)
    keys = {display_key(config, column) for column in group_by if column not in over} - {""}
    rolled = {display_key(config, column) for column in over} - {""} - keys
    return [
        column
        for column in group_by
        if (column not in over or column in keys) and column not in rolled
    ]


def coarse_call(
    query: Mapping[str, Any],
    over: Iterable[str],
    select: list[dict[str, Any]],
    config: PackageConfig,
    *,
    group_by: Iterable[str] | None = None,
    time: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The call at the coarser grain, selecting ``select``: the call's own ``where``, window,
    entity filters and route decisions, with ``group_by`` (and the period, when ``over`` names
    it) rolled up. No ``having``, ``order_by`` or ``limit``: those act on the call's rows."""
    over = list(over)
    payload: dict[str, Any] = {key: query[key] for key in _CARRIED if key in query}
    payload["select"] = select
    payload["group_by"] = coarse_group_by(
        query.get("group_by") or [] if group_by is None else group_by, over, config
    )
    # Entity filters cut the call's rows; a having (any other metric filter) acts after.
    payload["metric_filters"] = [
        item
        for item in query.get("metric_filters") or []
        if (item.get("expression") or {}).get("kind") == "metric_predicate"
    ]
    window = dict(query.get("time") or {} if time is None else time)
    window.pop("window_total", None)
    if TIME_AXIS in over:
        window = {key: value for key, value in window.items() if key not in _GRAIN_KEYS}
        if window.get("start") is None and window.get("end") is None:
            window = {}  # every period: the call reads no window
    if window:
        payload["time"] = window
    return payload


def _over_groups(
    totals: list[tuple[str, dict[str, Any]]],
) -> dict[frozenset[str], list[tuple[str, dict[str, Any]]]]:
    """Totals over the same columns share one coarser call."""
    groups: dict[frozenset[str], list[tuple[str, dict[str, Any]]]] = {}
    for alias, expression in totals:
        groups.setdefault(frozenset(expression.get("over") or []), []).append((alias, expression))
    return groups


def _unique_name(base: str, taken: set[str]) -> str:
    name, index = base, 1
    # Nested CTEs take the name as a prefix, so no taken name may contain it either.
    while any(name.casefold() in other for other in taken):
        index += 1
        name = f"{base}_{index}"
    taken.add(name.casefold())
    return name


def _row_sources(select: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The call's select items as its coarser call reads them: a share or a total is its input
    there (the same rows), every other item itself. The coarser call keeps every row source the
    call has, so it has a row wherever the call does, and settles the input's empty groups
    there as the call without the ``over`` columns would."""
    out = []
    for row in select:
        expression = row.get("expression")
        if isinstance(expression, Mapping) and expression.get("kind") in {"share", "total"}:
            expression = expression["input"]
        out.append(dict(expression or {}))
    return out


def total_sources(
    plan: LogicalPlan, config: PackageConfig, taken: set[str], *, table_alias: str = "base"
) -> tuple[list[SqlCte], list[SqlJoin], dict[str, SqlExpr]]:
    """The coarser calls of the plan's totals as CTEs, each LEFT-joined to the call's rows on
    the keys it keeps (NULL-safe, so a NULL group meets its own total), and each total's value
    by its alias.

    A coarser call is the call itself without the ``over`` columns: every select item (a share
    or total as its input), compiled standalone, so its empty groups settle as that call's do
    (loaded coverage, observation, data of nothing). A call that can't be answered at that
    grain refuses the total, as it would refuse on its own."""
    totals = selected_totals(plan.query.get("select") or [])
    ctes: list[SqlCte] = []
    joins: list[SqlJoin] = []
    values: dict[str, SqlExpr] = {}
    time_alias = time_output_alias(plan.time) if plan.time else ""
    sources = _row_sources(plan.query.get("select") or [])
    for over, items in _over_groups(totals).items():
        keys = coarse_group_by(plan.group_by, over, config)
        if time_alias and TIME_AXIS not in over and not plan.time.get("window_total"):
            keys.append(time_alias)  # a window total's time is no column
        name = _unique_name("coarse_total", taken)
        # Columns of the engine's own naming: a caller's alias could be an internal one.
        columns = [f"{name}_{index}" for index in range(1, len(sources) + 1)]
        by_alias = {
            str(row.get("as")): column
            for row, column in zip(plan.query.get("select") or [], columns, strict=True)
        }
        mine = {alias: by_alias[alias] for alias, _ in items}
        payload = coarse_call(
            plan.query,
            over,
            [
                {"expression": source, "as": column}
                for source, column in zip(sources, columns, strict=True)
            ],
            config,
            group_by=plan.group_by,
            time=plan.time,
        )
        with recording_outputs_as({column: alias for alias, column in mine.items()}):
            select = nested.query_sql(config, payload, standalone=True)
        produced = {field.alias for field in select.select}
        if produced - set(columns) != set(keys):
            raise SemanticLayerError(
                "REWRITE_NOT_SUPPORTED",
                f"The total in '{items[0][0]}' can't be computed at its coarser grain: the call "
                f"without {', '.join(sorted(over))} doesn't answer one row per "
                f"{' and '.join(keys) or 'call'}. Ask for that grain in its own call.",
                details={"totals": [alias for alias, _ in items], "over": sorted(over)},
            )
        ctes.append(SqlCte(name=name, query=_namespace_sql_select(select, f"{name}__")))
        joins.append(
            SqlJoin(
                join_type="LEFT",
                table=SqlTableRef(name=name, alias=name),
                on=_join_condition(keys, table_alias, name, dialect=_dialect(config)),
            )
        )
        values.update(
            (alias, SqlIdentifier(parts=[name, column])) for alias, column in mine.items()
        )
    return ctes, joins, values
