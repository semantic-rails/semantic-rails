"""Readiness: a period comparison never puts an incomplete period beside a complete one.

A draft compares periods when its Query IR, parsed as execution parses it, carries a
``prior_period`` expression anywhere (a select or a metric filter, inside arithmetic or not),
or reads a metric whose definition carries one, through any chain of metrics. Such a draft is
ready only when every period it
returns is complete at the request's ``now``: ``time.end`` falls on a boundary of the
``time.grain`` buckets, in the temporal role's zone, no later than ``now``. The periods it
compares against come earlier, so they are complete too. Without ``time.end`` the window
reaches ``now``, and its last period is the one in progress. The check reads the bounds it
can and holds whatever it can't: a non-default calendar's buckets, a ``where`` bound on a
date, which can cut a period short, or a clock it can't read.

``plan`` and the granted-metric plan both call it before offering ``ready_for: execute``.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .ast import (
    _floor_period,
    _parse_now,
    _shift_period,
    _time_spec_from_payload,
    every_filter,
    normalize_query,
)
from .errors import SemanticLayerError
from .expressions import MetricRecipeRefExpr, OffsetWindowExpr, PriorPeriodExpr
from .schema import base_of

_CODE = "PERIOD_COMPARISON_INCOMPLETE"
_SUB_DAY = {"hour": timedelta(hours=1), "minute": timedelta(minutes=1)}
_GRAINS = {"day", "week", "month", "quarter", "year", *_SUB_DAY}
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


def compares_periods(config: Any, query: dict[str, Any]) -> bool:
    """Whether the draft, parsed as execution parses it, reads a ``prior_period`` expression,
    its own or a metric's.

    Unknown means comparing: a draft that doesn't parse, or a metric whose definition plan
    can't read, counts as one.
    """

    try:
        parsed = normalize_query(query, config=config)
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
        elif isinstance(node, PriorPeriodExpr) or (
            isinstance(node, OffsetWindowExpr) and node.kind == "prior_period"
        ):
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
    query: dict[str, Any],
    *,
    start: Any = "",
    policy_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The hold for a period comparison whose periods are not all complete at ``now``.

    ``start`` is the question's window start a draft left out (``TIME_WINDOW_START_DROPPED``):
    the complete-periods alternative keeps the rows from it. ``policy_context`` carries the
    request's ``now``; without one, the check reads the clock execution would.
    """

    if not compares_periods(config, query):
        return None
    raw_time = query.get("time")
    time: dict[str, Any] = raw_time if isinstance(raw_time, dict) else {}
    grain = str(time.get("grain", "") or "").strip().lower()
    role = next((row for row in config.temporal_roles if row.id == time.get("temporal_role")), None)
    zone = str(getattr(role, "timezone", "") or "UTC")
    try:
        now = _local(_parse_now(policy_context), zone)
    except (SemanticLayerError, ValueError, KeyError):
        return _hold(
            "The draft compares periods, but the request's now can't be read, so plan can't "
            "tell that each period is complete.",
            {"path": "policy_context.now"},
            "Pass policy_context.now as an ISO date or timestamp, then plan again.",
        )
    now_text = f"{now.isoformat(timespec='seconds')} {zone}"
    calendar = str(time.get("calendar_id", "") or "default").strip()
    if calendar.lower() != "default":
        return _hold(
            f"The draft compares periods on calendar '{calendar}', and plan can't read where "
            f"that calendar's periods end, so it can't tell that each one is complete at now "
            f"({now_text}).",
            {"path": "time.calendar_id", "calendar_id": calendar, "now": now_text},
            "Compare periods on the default calendar instead: drop query.time.calendar_id and "
            f"{_set_end(time, 'a period end no later than now')}, then plan again.",
        )
    if grain not in _GRAINS:
        return _hold(
            f"The draft compares periods, but its time.grain ('{grain}') doesn't say which "
            f"periods it returns, so plan can't tell that each one is complete at now "
            f"({now_text}).",
            {"path": "time.grain", "grain": grain, "now": now_text},
            "Set query.time.grain to the period the comparison reports, then plan again.",
        )
    dated = sorted(
        {
            str(row.get("field"))
            for row in every_filter(query.get("where"))
            if isinstance(row, dict) and _is_dated(config, row.get("field"))
        }
    )
    try:
        spec = _time_spec_from_payload(time, policy_context=policy_context, config=config)
        end = _local(_parse_bound(spec.end), zone) if spec is not None and spec.end else None
        reason = "the draft has no time.end, so its window runs to now"
        # The alternative replaces a range, so it keeps the rows from the range's start.
        kept = start or (spec.start if spec is not None and "range" in time else "")
    except (SemanticLayerError, ValueError, OverflowError):
        end, reason, kept = None, "plan can't read its time.end", start  # proves no boundary
    try:
        first = _local(_parse_bound(kept), zone) if kept else None
    except ValueError:
        first = None
    current = _floor(now, grain)
    if end is not None and end <= now and _floor(end, grain) == end and not dated:
        return None
    if dated:
        complete_end = current if end is None or end > now else _floor(end, grain)
        return _hold(
            f"The draft compares periods and bounds time with a where filter on "
            f"{', '.join(dated)}, which can cut a {grain} short, so plan can't tell that each "
            f"{grain} is complete at now ({now_text}).",
            {"path": "where", "fields": dated, "grain": grain, "now": now_text},
            _alternative(time, grain, complete_end, first),
        )
    if end is None or end > now:
        incomplete = complete_end = current
        if end is not None:
            reason = f"the window runs to {_iso(end, grain)}"
    else:
        incomplete = complete_end = _floor(end, grain)
        reason = f"time.end {_iso(end, grain)} cuts it short"
    return _hold(
        f"The draft compares each {grain} with an earlier period, but {_label(incomplete, grain)} "
        f"is not complete at now ({now_text}): {reason}. It would sit beside a complete period "
        "as if it were one.",
        {
            "path": "time.end",
            "grain": grain,
            "incomplete_period": {
                "start": _iso(incomplete, grain),
                "end": _iso(_next(incomplete, grain), grain),
            },
            "now": now_text,
            "complete_end": _iso(complete_end, grain),
            **({"requested_start": str(start)} if start else {}),
        },
        _alternative(time, grain, complete_end, first),
    )


def _hold(message: str, details: dict[str, Any], hint: str) -> dict[str, Any]:
    return {
        "code": _CODE,
        "message": message,
        "details": details,
        "recovery_hints": [{"kind": "compare_complete_periods", "message": hint}],
    }


def _alternative(
    time: dict[str, Any], grain: str, complete_end: datetime, first: datetime | None
) -> str:
    """The complete-periods alternative: every period up to the last one that has ended,
    from the question's dropped start or the range's start (``first``) on."""

    last = _label(_previous(complete_end, grain), grain)
    bound = f"{_set_end(time, _iso(complete_end, grain))} (end-exclusive)"
    if first is not None and first >= complete_end:
        return (
            f"No {grain} from {_iso(first, grain)} on is complete yet. Compare complete "
            f"{grain}s through {last} instead: {bound}, then plan again."
        )
    keep = f"; keep the rows dated {_iso(first, grain)} or later" if first else ""
    return f"Compare complete {grain}s through {last}: {bound}, then plan again{keep}."


def _set_end(time: dict[str, Any], end: str) -> str:
    """Bound the window at ``end``. A range takes no ``time.end`` beside it, and a
    ``time.start`` would cut the earlier periods a comparison reads, so the end replaces it."""

    if "range" in time:
        return f"replace query.time.range with query.time.end set to {end}"
    return f"set query.time.end to {end}"


def _is_dated(config: Any, field: Any) -> bool:
    dimension = next((row for row in config.dimensions if row.id == field), None)
    return str(getattr(dimension, "data_type", "") or "").lower() in {"date", "timestamp"}


def _parse_bound(value: Any) -> date | datetime:
    if isinstance(value, (date, datetime)):
        return value
    return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))


def _local(value: date | datetime, zone: str) -> datetime:
    """A moment as wall-clock time in the role's zone, where the buckets are cut."""

    if not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    if value.tzinfo is not None:
        value = value.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    return value


def _floor(moment: datetime, grain: str) -> datetime:
    if grain == "hour":
        return moment.replace(minute=0, second=0, microsecond=0)
    if grain == "minute":
        return moment.replace(second=0, microsecond=0)
    day = _floor_period(moment.date(), grain)
    return datetime(day.year, day.month, day.day)


def _next(start: datetime, grain: str) -> datetime:
    if grain in _SUB_DAY:
        return start + _SUB_DAY[grain]
    day = _shift_period(start.date(), grain, 1)
    return datetime(day.year, day.month, day.day)


def _previous(end: datetime, grain: str) -> datetime:
    if grain in _SUB_DAY:
        return end - _SUB_DAY[grain]
    day = _shift_period(end.date(), grain, -1)
    return datetime(day.year, day.month, day.day)


def _iso(moment: datetime, grain: str) -> str:
    if grain in _SUB_DAY or moment.time() != datetime.min.time():
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
    if grain == "day":
        return start.date().isoformat()
    return f"the {grain} from {start.isoformat(timespec='minutes')}"


__all__ = ["compares_periods", "incomplete_period_why"]
