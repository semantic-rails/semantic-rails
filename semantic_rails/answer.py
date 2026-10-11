"""The answer envelope: what a query, validate or compile response says beside its rows.

``answer_fields`` reads from the compiled call (and, for a query, its rows and loaded
coverage) what every answer states: ``window``, ``clock``, ``observation_scope`` (always, since
it decides 0 or NULL), ``routes`` (only when a route was chosen) and the assumptions the
call's time and entity filters imply. ``finish`` completes a response: a warning whose code
the registry lists as an assumption is stated under ``assumptions``, the echoed query is the
call, and empty fields go. Verbosity trims only afterwards
(``runtime_parts.responses.apply_response_verbosity``) and never removes a warning, an
assumption or a route.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .ast import PERIOD_PARTIAL, TIME_AXIS
from .clock import describe_window, engine_window, format_now, local_date, period_is_partial
from .compiler import read_routes
from .compiler_parts.empty_groups import observation_scope
from .errors import SemanticLayerError
from .fanout import offered_rows, package_route, route_note
from .issues import ASSUMPTION, CODES
from .query_call import call_from_query, call_where

# Said when a time window with no grain was aggregated as one total.
WINDOW_TOTAL_MESSAGE = (
    "The time window had no grain, so it was aggregated as one total over the window with no "
    "time column. Set time.grain to get one row per period."
)

_OVER_MEANING = {
    "all_time": "over all of its rows, ignoring the query window",
    "window": "over its rows inside the query window",
    "period": "per output period",
}
_EMPTY: tuple[Any, ...] = (None, "", [], {})
# An empty result is still an answer.
_KEPT_EMPTY = frozenset({"rows"})


def mark_partial_periods(
    rows: list[dict[str, Any]], compiled: Mapping[str, Any], *, now: datetime, zone: str
) -> None:
    """``period_partial`` on each row of a grain series, right after ``period``: whether the
    row's period ends after ``now`` in the clock's zone."""
    grain = (compiled["logical_plan"].time or {}).get("grain")
    if not grain:
        return
    for row in rows:
        period = row.get(TIME_AXIS)
        if isinstance(period, datetime) and period.utcoffset() is not None:
            period = period.astimezone(ZoneInfo(zone))
        marked: dict[str, Any] = {}
        for key, value in row.items():
            if key != PERIOD_PARTIAL:
                marked[key] = value
            if key == TIME_AXIS:
                marked[PERIOD_PARTIAL] = period_is_partial(period, grain, now, zone)
        row.clear()
        row.update(marked)


def answer_fields(
    config: Any,
    compiled: Mapping[str, Any],
    *,
    now: datetime,
    zone: str,
    coverage: Mapping[str, Any] | None = None,
    rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """What an answer's time, routes and filters mean, said on every response.

    ``window`` ({start, end, now, partial, data_through}, only the keys that apply) whenever a
    window or a grain series was resolved; ``data_through`` is the date, in the clock's zone, of
    the last row at or before ``now`` that the answer's coverage read (``coverage``), so it is
    known only once the query ran on a warehouse with loaded coverage. ``clock`` whenever a
    time-bearing measure was read. A ``this`` window is assumed partial ("October 2026 so far
    (8 of 31 days)").
    """
    plan = compiled["logical_plan"]
    time = plan.time or {}
    out: dict[str, Any] = {}
    assumptions: list[dict[str, Any]] = []
    if time.get("window_total"):
        assumptions.append({"code": "WINDOW_TOTAL", "message": WINDOW_TOTAL_MESSAGE})
    assumptions.extend(_entity_filter_over(plan.query))
    if time.get("grain") or time.get("start") is not None or time.get("end") is not None:
        window = engine_window(time, now=now, timezone=zone)
        # A ``this`` window ends at now: state that instant with its offset, as ``now`` is.
        end = _instant(now, zone) if "this" in window.spec else window.end
        shown = {"start": window.start, "end": end, "now": format_now(now)}
        out["window"] = {key: value for key, value in shown.items() if value is not None}
        if window.start is not None or window.end is not None:
            out["window"]["partial"] = window.partial
        elif rows:
            out["window"]["partial"] = any(row.get(PERIOD_PARTIAL) for row in rows)
        edges = [
            value
            for key, value in (coverage or {}).items()
            if key.endswith("__data_through") and isinstance(value, datetime)
        ]
        if edges:
            out["window"]["data_through"] = local_date(min(edges), zone)
        if "this" in window.spec:
            text = describe_window(window, now=now, timezone=zone)
            assumptions.append({"code": "PARTIAL_PERIOD", "message": text})
    clocks = (
        {time["temporal_role"]}
        if time.get("temporal_role")
        else {row.temporal_role for row in plan.bound_measures if row.temporal_role}
    )
    if len(clocks) == 1:
        out["clock"] = clocks.pop()
    out["observation_scope"] = observation_scope(plan.query, config)
    out["routes"] = routes_taken(config, compiled)
    out["assumptions"] = assumptions
    return out


def _instant(now: datetime, zone: str) -> str:
    """``now`` as wall time in the clock's zone with its offset (``Z`` for UTC)."""
    local = now.astimezone(ZoneInfo(zone)).isoformat(timespec="seconds")
    return local.replace("+00:00", "Z")


def _entity_filter_over(query: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each entity filter's ``over``: how each entity's aggregate was taken."""
    return [
        {
            "code": "ENTITY_FILTER_OVER",
            "entity": row["entity"],
            "over": row["over"],
            "message": f"Each {row['entity']}'s aggregate was taken {_OVER_MEANING[row['over']]}.",
        }
        for row in call_where([], query.get("metric_filters"))
    ]


def routes_taken(config: Any, compiled: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each entity pair whose route was chosen among two or more, as ``{from, to, via}``. A pair
    with one route had nothing to choose and is not listed.

    A route the call chose among the options of the pair's refusal names up to three of the
    other routes it offered (``other_routes``, each ready to send as ``routes``; the
    rest counted in ``more_routes``): one-step switches, and no alternative ever runs. A
    compact or full response also says how each route was chosen (``basis``): by the call's
    ``routes`` (with the package's basis it ``replaced``), by the start's own key
    (``own_key``, with the row that would make each other route the default in
    ``alternatives`` when it would load, else the rows it disagrees with in
    ``conflicts_with``), or by the package's recorded rows (``recorded``, with the pairs whose
    rows it follows in ``recorded_for`` when it inherits them)."""
    taken: list[dict[str, Any]] = []
    for row in compiled.get("route_decisions") or []:
        start, target, path = row["source_entity"], row["target_entity"], row["relationship_path"]
        route: dict[str, Any] = {"from": start, "to": target, "via": list(path)}
        offered = _offered(config, start, target) if row["replaced"] == "undecided" else []
        if list(path) in offered:
            others = [other for other in offered if other != list(path)]
            route["other_routes"] = [
                {"from": start, "to": target, "via": list(other)} for other in others[:3]
            ]
            route["more_routes"] = len(others) - 3 if len(others) > 3 else None
        route.update(basis="routes", replaced=row["replaced"])
        taken.append(route)
    seen = {(row["from"], row["to"]) for row in taken}
    for start, target, path in read_routes(
        compiled["logical_plan"], compiled.get("route_choices") or []
    ):
        resolution = None if (start, target) in seen else route_note(config, start, target, path)
        if resolution is None:
            continue
        seen.add((start, target))
        route = {"from": start, "to": target, "via": list(resolution.routes[0])}
        if resolution.basis == "colocated_key":
            alternatives, conflicts = offered_rows(config, start, target, resolution.routes[1:])
            route.update(basis="own_key", alternatives=alternatives, conflicts_with=conflicts)
        else:
            inherited = [{"from": source, "to": end} for source, end in resolution.rows]
            route.update(basis="recorded", recorded_for=inherited)
        taken.append(route)
    return [{key: value for key, value in route.items() if value not in _EMPTY} for route in taken]


# What a minimal response keeps of each route.
MINIMAL_ROUTE_KEYS = ("from", "to", "via", "other_routes", "more_routes")


def _offered(config: Any, start: str, target: str) -> list[list[str]]:
    """The routes the pair's AMBIGUOUS_PATH refusal offers the caller (none when it has none)."""
    try:
        package_route(config, start=start, target=target)
    except SemanticLayerError as exc:
        if exc.code == "AMBIGUOUS_PATH":
            options = exc.details.get("clarification", {}).get("options", [])
            return [list(option["relationship_path"]) for option in options]
    return []


def finish(
    out: dict[str, Any],
    *,
    payload: Mapping[str, Any],
    compiled: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A response as a caller reads it, before verbosity trims it.

    A warning whose code ``issues.CODES`` lists as an assumption (a disclosure any part of the
    engine writes) moves to ``assumptions`` as ``{code, message, **details}``: registering a
    code there is all a new disclosure needs. ``query`` and ``normalized_query`` echo the call
    (``call_from_query``), never the engine form with its identity and clock. Empty fields go,
    from the response (except ``rows``) and from each warning."""
    warnings = list(out.get("warnings") or [])
    stated = [row for row in warnings if CODES.get(str(row.get("code"))) == ASSUMPTION]
    out["warnings"] = [
        {key: value for key, value in row.items() if value not in _EMPTY}
        for row in warnings
        if row not in stated
    ]
    out["assumptions"] = [
        *(out.get("assumptions") or []),
        *(
            {
                "code": row["code"],
                "message": row.get("message", ""),
                **dict(row.get("details") or {}),
            }
            for row in stated
        ),
    ]
    out["query"] = _call(payload)
    if "normalized_query" in out and compiled is not None:
        out["normalized_query"] = _call(compiled["explain"].normalized_query or {})
    return {key: value for key, value in out.items() if key in _KEPT_EMPTY or value not in _EMPTY}


def _call(query: Mapping[str, Any]) -> dict[str, Any] | None:
    """The call that states ``query``; None when it states none (a refused call)."""
    try:
        return call_from_query(query)
    except (SemanticLayerError, KeyError, TypeError, AttributeError):
        return None
