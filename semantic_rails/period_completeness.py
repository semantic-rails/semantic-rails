"""Readiness: a period comparison never puts an incomplete period beside a complete one.

A draft compares periods when its call, read as execution reads it, carries a ``prior_period``
expression anywhere (a select or a ``having``, inside arithmetic or not), or reads a metric whose
definition carries one, through any chain of metrics. A comparison takes no window but an end
(validation refuses one beside ``last``, ``this`` or a ``start``), so it is ready only when the
earliest bound that ends its window (``time.end``, or a ``where`` upper bound on the clock's own
date) falls on a boundary of the ``time.grain`` buckets, in the clock's zone, no later than
``now``. The periods it compares against come earlier, so they are complete too. Without a
bound the window runs to ``now``, and its last period is the one in progress.

Every clock the draft buckets by counts, as the compiler binds each measure, through metrics
and ``having``; clocks in different zones end their periods at different moments, so they hold.
The check holds whatever it can't read exactly, with no option: a draft it can't plan, one that
also reads an entity's aggregate (a metric predicate, an entity filter's having, a scoped
aggregate's conditions), a measure's own filter on a date or time, a ``where`` bound on another
date or on a clock it can't line up, a bound with a UTC offset (other than UTC on a UTC clock)
or a comma before its fraction, or a grain or clock it can't read; a bound finer than a
microsecond holds with the option below it. Its complete-periods option keeps the draft's filters and ends at the last
boundary before every bound; it is offered only when the same check calls it complete.

``plan``, the granted-metric plan and a matched package example all call it, on the draft's
engine form (``Runtime.engine_payload``, which carries ``now``), before offering
``ready_for: execute``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .ast import normalize_query
from .clock import UNITS, floor_period, local_now, parse_now, shift_period
from .compiler_parts.bind import _bound_filter_clauses
from .compiler_parts.paths import _leaf_time_role
from .compiler_parts.planning import plan_query
from .errors import SemanticLayerError
from .expressions import (
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    OffsetWindowExpr,
    ScopedAggregateExpr,
)
from .schema import base_of

_CODE = "PERIOD_COMPARISON_INCOMPLETE"
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
# Engine keys of a draft that aren't its query.
_REQUEST_KEYS = {"policy_context", "verbosity", "sql_profile", "limits", "request_id", "debug"}


def compares_periods(config: Any, query: Mapping[str, Any]) -> bool:
    """Whether the draft's engine form, parsed as execution parses it, reads a
    ``prior_period`` expression, its own or a metric's.

    Unknown means comparing: a draft that doesn't parse, or a metric whose definition plan
    can't read, counts as one.
    """

    try:
        parsed = normalize_query(
            {key: value for key, value in query.items() if key not in _REQUEST_KEYS},
            config=config,
        )
    except Exception:  # noqa: BLE001 — a draft that doesn't parse can't prove none
        return True
    recipes = {str(row.id): row for row in getattr(base_of(config), "metric_recipes", []) or []}
    seen: set[str] = set()
    pending: list[Any] = [
        *(row.expression for row in parsed.select),
        *(row.expression for row in parsed.metric_filters),
    ]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, OffsetWindowExpr) and node.kind == "prior_period":
            return True
        elif isinstance(node, MetricRecipeRefExpr):
            if node.metric_recipe not in seen:
                seen.add(node.metric_recipe)
                expression = getattr(recipes.get(node.metric_recipe), "expression", None)
                if not is_dataclass(expression):
                    return True
                pending.append(expression)
        elif is_dataclass(node):
            pending.extend(getattr(node, item.name) for item in fields(node))
    return False


def incomplete_period_why(
    config: Any,
    query: Mapping[str, Any],
    *,
    call: Mapping[str, Any] | None = None,
    start: Any = "",
) -> dict[str, Any] | None:
    """The hold for a period comparison whose periods are not all complete at ``now``.

    ``query`` is the draft's engine form, whose ``policy_context.now`` is the request's clock;
    ``call`` is the draft as the caller sends it, for the complete-periods option. ``start`` is
    the question's window start a draft left out (``TIME_WINDOW_START_DROPPED``): the
    alternative keeps the rows from it. The option is offered only when the same check calls
    it complete.
    """

    if not compares_periods(config, query):
        return None
    hold = _check(config, query, start)
    if hold is None:
        return None
    option = None
    if hold.cutoff is not None and call is not None:
        time = dict(query.get("time") or {})
        bounded = {**query, "time": {**time, "end": _iso(hold.cutoff)}}
        if _check(config, bounded, "") is None:
            option = _option(call, hold.grain, hold.cutoff)
    hint = hold.hint
    if hold.cutoff is not None and option is None and call is not None:
        hint = (
            f"Plan can't offer a call over complete {hold.grain}s for this draft: bound it with "
            "time.end on a period boundary no later than now, then plan again."
        )
    return _hold(hold.message, hold.details, hint, option)


@dataclass(frozen=True)
class _Held:
    message: str
    details: dict[str, Any]
    hint: str
    grain: str = ""
    # The end of the last complete period an option may stop at, when one exists.
    cutoff: datetime | None = None


def _check(config: Any, query: Mapping[str, Any], start: Any) -> _Held | None:
    """Why the draft's periods aren't all complete at ``now``, read on every clock it buckets
    by and every bound that ends its window; None when they are."""

    raw_time = query.get("time")
    time: Mapping[str, Any] = raw_time if isinstance(raw_time, Mapping) else {}
    grain = str(time.get("grain", "") or "").strip().lower()
    effective = _effective_roles(config, query)
    if effective is None:
        return _Held(
            "The draft compares periods, but plan can't read which clocks it buckets them by, "
            "so it can't tell that each period is complete.",
            {"path": "time.temporal_role"},
            "Validate the call, then plan again.",
        )
    roles, unproven = effective
    if unproven:
        return _unproven(f"it reads {unproven}", "select")
    zones = sorted({str(role.timezone or "UTC") for role in roles})
    if len(zones) > 1:
        return _Held(
            "The draft compares periods on clocks in different time zones "
            f"({', '.join(zones)}), whose periods end at different moments, so plan can't tell "
            "that each period it returns is complete.",
            {"path": "time.temporal_role", "timezones": zones},
            "Compare periods on clocks that share one time zone, then plan again.",
        )
    zone = zones[0] if zones else "UTC"
    try:
        instant = parse_now((query.get("policy_context") or {}).get("now"))
        now = local_now(instant, zone)
    except (SemanticLayerError, ValueError, OverflowError):
        return _Held(
            "The draft compares periods, but the request's now can't be read, so plan can't "
            "tell that each period is complete.",
            {"path": "now"},
            "Send now as an ISO date or date-time, then plan again.",
        )
    now_text = f"{now.isoformat(timespec='seconds')} {zone}"
    if grain not in UNITS:
        return _Held(
            f"The draft compares periods, but its time.grain ('{grain}') doesn't say which "
            f"periods it returns, so plan can't tell that each one is complete at now "
            f"({now_text}).",
            {"path": "time.grain", "grain": grain, "now": now_text},
            "Set time.grain to the period the comparison reports, then plan again.",
        )
    current = _floor(now, grain)
    bounds, uncut, unread = _bounds(config, query, roles, zone)
    if unread:
        source, problem = unread[0]
        return _unproven(f"{source} {problem}", _path(source))
    if uncut:
        return _Held(
            f"The draft compares periods and bounds time with a where filter on "
            f"{', '.join(uncut)}, which can cut a {grain} short where plan can't tell, so it "
            f"can't tell that each {grain} is complete at now ({now_text}).",
            {"path": "where", "fields": uncut, "grain": grain, "now": now_text},
            f"Plan can't tell which {grain}s the where filter on {', '.join(uncut)} leaves "
            "whole, so it offers no call over complete periods; compare periods the filter "
            "can't cut, then plan again.",
        )
    try:
        first = _local(_parse_bound(start)[0], zone) if start else None
    except (ValueError, OverflowError):
        first = None
    ends: list[tuple[datetime, str]] = []
    inexact: list[str] = []
    for source, bound in bounds:
        if bound is None:
            reason = f"plan can't read {source}"
            return _held(grain, now_text, current, current, reason, first, start)
        ends.append((bound[0], source))
        if not bound[1]:
            inexact.append(source)
    if inexact:
        cut = min([now, *(moment for moment, _ in ends)])
        return _held(
            grain,
            now_text,
            _floor(cut, grain),
            _floor(cut, grain),
            f"{inexact[0]} is finer than a microsecond, which plan can't read exactly",
            first,
            start,
            path=_path(inexact[0]),
        )
    if not ends:
        reason = "the draft has no time.end, so its window runs to now"
        return _held(grain, now_text, current, current, reason, first, start)
    end, source = min(ends, key=lambda item: item[0])
    if end <= now and _floor(end, grain) == end:
        return None
    if end > now:
        reason = f"the window runs to {_iso(end)}"
        return _held(grain, now_text, current, current, reason, first, start, path=_path(source))
    reason = f"{source} {_iso(end)} cuts it short"
    cut = _floor(end, grain)
    return _held(grain, now_text, cut, cut, reason, first, start, path=_path(source))


def _unproven(reason: str, path: str) -> _Held:
    """A hold plan can't lift with a call of its own: nothing it can offer is provably
    complete."""
    return _Held(
        f"The draft compares periods, but {reason}, so plan can't tell that each period it "
        "returns is complete.",
        {"path": path},
        "Plan can't prove this comparison's periods complete, so it offers no call; compare "
        "periods on one clock, bounded by time.end on a period boundary, then plan again.",
    )


# A bound's UTC offset or zone designator, after its time of day.
_DESIGNATOR = re.compile(r"(?:[zZ]|[+-]\d{2}(?::?\d{2}){0,2}(?:\.\d+)?)$")
_UTC_DESIGNATORS = {"Z", "+00:00", "+0000", "+00"}


def _bound_problem(value: Any, utc: bool) -> str:
    """Why a bound can't be read as the SQL reads it: a comma before its fraction, or a UTC
    offset (the SQL compares the wall time) other than UTC itself on a UTC clock; "" when
    none."""
    text = str(value).strip()
    if "," in text:
        return "writes its fraction after a comma, which plan can't read exactly"
    designator = _DESIGNATOR.search(text[11:]) if len(text) > 10 and text[10] in "Tt " else None
    if designator and not (utc and designator.group(0).upper() in _UTC_DESIGNATORS):
        return "carries a UTC offset, which the query compares as wall time"
    return ""


def _held(
    grain: str,
    now_text: str,
    incomplete: datetime,
    complete_end: datetime,
    reason: str,
    first: datetime | None,
    start: Any,
    *,
    path: str = "time.end",
) -> _Held:
    return _Held(
        f"The draft compares each {grain} with an earlier period, but "
        f"{_label(incomplete, grain)} is not complete at now ({now_text}): {reason}. It would "
        "sit beside a complete period as if it were one.",
        {
            "path": path,
            "grain": grain,
            "incomplete_period": {
                "start": _iso(incomplete),
                "end": _iso(_shift(incomplete, grain, 1)),
            },
            "now": now_text,
            "complete_end": _iso(complete_end),
            **({"requested_start": str(start)} if start else {}),
        },
        _alternative(grain, complete_end, first),
        grain,
        complete_end,
    )


def _path(source: str) -> str:
    return "time.end" if source == "time.end" else "where"


def _effective_roles(config: Any, query: Mapping[str, Any]) -> tuple[list[Any], str] | None:
    """The clock each measure the draft reads is bucketed by, through metrics and ``having``,
    as the compiler binds it, with what plan can't check about those reads ("" when nothing);
    None when the draft can't be planned."""

    payload = {key: value for key, value in query.items() if key not in _REQUEST_KEYS}
    try:
        plan = plan_query(config, None, payload)
        normalized = normalize_query(payload, config=config)
        names = {_leaf_time_role(bound, normalized, config) for bound in plan.bound_measures}
        unproven = _unchecked_reads(config, normalized, plan.bound_measures)
    except Exception:  # noqa: BLE001 — a draft that can't be planned proves no clock
        return None
    raw_time = query.get("time")
    requested = raw_time.get("temporal_role") if isinstance(raw_time, Mapping) else None
    names = (names - {""}) or {str(requested or "")}
    roles = [row for row in config.temporal_roles if row.id in names]
    return (roles, unproven) if len(roles) == len(names) else None


def _unchecked_reads(config: Any, parsed: Any, leaves: list[Any]) -> str:
    """What the draft reads that plan can't check period by period: an entity's aggregate (an
    entity filter's having, a metric predicate, a scoped aggregate's conditions), which reads
    periods of its own, or a measure's own filter on a date, which can cut a period short.
    "" when there is none."""

    recipes = {str(row.id): row for row in getattr(base_of(config), "metric_recipes", []) or []}
    seen: set[str] = set()
    pending: list[Any] = [
        *(row.expression for row in parsed.select),
        *(row.expression for row in parsed.metric_filters),
        *parsed.where,
    ]
    while pending:
        node = pending.pop()
        if isinstance(node, (list, tuple)):
            pending.extend(node)
        elif isinstance(node, MetricPredicateExpr) or (
            isinstance(node, ScopedAggregateExpr) and (node.predicates or node.where)
        ):
            return "an entity's aggregate, which reads periods of its own"
        elif isinstance(node, MetricRecipeRefExpr):
            if node.metric_recipe not in seen:
                seen.add(node.metric_recipe)
                pending.append(getattr(recipes.get(node.metric_recipe), "expression", None))
        elif is_dataclass(node) and not isinstance(node, type):
            pending.extend(getattr(node, item.name) for item in fields(node))
    dimensions = {row.id: row for row in config.dimensions}
    for leaf in leaves:
        spec = dict(leaf.filter_spec or {})
        clauses = list(spec.get("all", []) or [])
        if set(spec) - {"all"} or any(clause.get("expression") is not None for clause in clauses):
            return "an entity's aggregate, which reads periods of its own"
        for clause in _bound_filter_clauses(leaf, config):
            if _is_dated(dimensions.get(str(clause.get("field")))):
                return (
                    f"{leaf.measure_id}'s own filter on {clause['field']}, which can cut a period"
                )
    return ""


# A where bound's ops: the one a comparison can run beside is an upper bound.
_UPPER = {"lt": "lt", "<": "lt", "lte": "lte", "<=": "lte"}


def _bounds(
    config: Any, query: Mapping[str, Any], roles: list[Any], zone: str
) -> tuple[list[tuple[str, tuple[datetime, bool] | None]], list[str], list[tuple[str, str]]]:
    """Every bound that ends the window, by source (``time.end``, or the where filter on a
    field), as (wall time in the clock's zone, read exactly), None for one plan can't read; and
    the dated fields of where filters whose cut plan can't line up with the clock's buckets."""

    raw_time = query.get("time")
    time: Mapping[str, Any] = raw_time if isinstance(raw_time, Mapping) else {}
    bounds: list[tuple[str, tuple[datetime, bool] | None]] = []
    utc = all(
        str(role.timezone or "UTC") == "UTC" and str(role.column_timezone or "UTC") == "UTC"
        for role in roles
    )
    unread: list[tuple[str, str]] = []
    if time.get("end") is not None and (problem := _bound_problem(time["end"], utc)):
        unread.append((f"time.end {time['end']}", problem))
    if time.get("end") is not None:
        try:
            moment, exact = _parse_bound(time["end"])
            bounds.append(("time.end", (_local(moment, zone), exact)))
        except (ValueError, OverflowError):
            bounds.append(("time.end", None))
    dimensions = {row.id: row for row in config.dimensions}
    clocks = {
        (dimensions[role.dimension].entity, dimensions[role.dimension].column)
        for role in roles
        if role.dimension in dimensions
    }
    uncut: set[str] = set()
    for item in query.get("where") or []:
        nested = not (isinstance(item, Mapping) and "field" in item)
        for row in _rows(item):
            dimension = dimensions.get(str(row.get("field")))
            if dimension is None or not _is_dated(dimension):
                continue
            field, op = str(row.get("field")), _UPPER.get(str(row.get("op", "")).lower())
            if (
                nested
                or op is None
                or len(roles) != 1
                or not utc
                or (dimension.entity, dimension.column) not in clocks
            ):
                uncut.add(field)
                continue
            source = f"the where filter on {field}"
            if problem := _bound_problem(row.get("value"), utc):
                unread.append((source, problem))
                continue
            try:
                value, exact = _parse_bound(row.get("value"))
            except (ValueError, OverflowError, TypeError):
                bounds.append((source, None))
                continue
            moment = _local(value, zone)
            if op == "lte":  # through the value: a DATE through its whole day
                pure_date = str(row.get("value")).strip()[10:] == ""
                day = str(getattr(dimension, "data_type", "")).lower() == "date"
                moment += timedelta(days=1) if day and pure_date else timedelta(microseconds=1)
            bounds.append((source, (moment, exact)))
    return bounds, sorted(uncut), unread


def _rows(item: Any) -> list[Mapping[str, Any]]:
    """Every condition row (``field``, ``op``, ``value``) in a where item, nested ones too."""

    if isinstance(item, Mapping):
        if "field" in item:
            return [item]
        return [row for value in item.values() for row in _rows(value)]
    if isinstance(item, list):
        return [row for value in item for row in _rows(value)]
    if is_dataclass(item) and not isinstance(item, type):
        return [row for entry in fields(item) for row in _rows(getattr(item, entry.name))]
    return []


def _hold(
    message: str,
    details: dict[str, Any],
    hint: str,
    option: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": _CODE,
        "message": message,
        "details": {**details, **({"options": [option]} if option else {})},
        "recovery_hints": [{"kind": "compare_complete_periods", "message": hint}],
    }


def _option(
    call: Mapping[str, Any] | None, grain: str, complete_end: datetime
) -> dict[str, Any] | None:
    """The draft over the complete periods: its own call, ending where the last one ends."""

    if call is None:
        return None
    time = dict(call.get("time") or {})
    return {
        "call": {**call, "time": {**time, "end": _iso(complete_end)}},
        "reads": f"every {grain} through {_label(_shift(complete_end, grain, -1), grain)}",
    }


def _alternative(grain: str, complete_end: datetime, first: datetime | None) -> str:
    """The complete-periods alternative: every period up to the last one that has ended,
    from the question's dropped start (``first``) on."""

    last = _label(_shift(complete_end, grain, -1), grain)
    bound = f"set time.end to {_iso(complete_end)} (end-exclusive)"
    if first is not None and first >= complete_end:
        return (
            f"No {grain} from {_iso(first)} on is complete yet. Compare complete {grain}s "
            f"through {last} instead: {bound}, then plan again."
        )
    keep = f"; keep the rows dated {_iso(first)} or later" if first else ""
    return f"Compare complete {grain}s through {last}: {bound}, then plan again{keep}."


def _is_dated(dimension: Any) -> bool:
    return str(getattr(dimension, "data_type", "") or "").lower() in {
        "date",
        "datetime",
        "time",
        "timestamp",
    }


# Fractional seconds past the sixth digit, which Python's datetime drops.
_FRACTION = re.compile(r"\.(\d+)")


def _parse_bound(value: Any) -> tuple[date | datetime, bool]:
    """A bound, and whether Python reads it exactly (no fraction finer than a microsecond)."""

    if isinstance(value, (date, datetime)):
        return value, True
    text = str(value).strip()
    fraction = _FRACTION.search(text)
    exact = fraction is None or not fraction.group(1)[6:].strip("0")
    return datetime.fromisoformat(text.replace("Z", "+00:00")), exact


def _local(value: date | datetime, zone: str) -> datetime:
    """A moment as wall-clock time in the clock's zone, where the buckets are cut."""

    if not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    if value.tzinfo is not None:
        return local_now(value, zone)
    return value


def _floor(moment: datetime, grain: str) -> datetime:
    day = floor_period(moment.date(), grain)
    return datetime(day.year, day.month, day.day)


def _shift(start: datetime, grain: str, periods: int) -> datetime:
    day = shift_period(start.date(), grain, periods)
    return datetime(day.year, day.month, day.day)


def _iso(moment: datetime) -> str:
    if moment.time() != datetime.min.time():
        return moment.isoformat(timespec="seconds")
    return moment.date().isoformat()


def _label(start: datetime, grain: str) -> str:
    if grain == "year":
        return str(start.year)
    if grain == "quarter":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    if grain == "month":
        return f"{_MONTHS[start.month - 1]} {start.year}"
    if grain == "week":
        return f"the week of {start.date().isoformat()}"
    return start.date().isoformat()


__all__ = ["compares_periods", "incomplete_period_why"]
