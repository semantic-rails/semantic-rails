"""Readiness: a period comparison never puts an incomplete period beside a complete one.

A draft compares periods when its Query IR carries a ``prior_period`` expression anywhere
(a select or a metric filter, inside arithmetic or not), or reads a metric whose definition
carries one, through any chain of metrics. Such a draft is ready only when every period it
returns is complete at the request's ``now``: ``time.end`` falls on a boundary of the
``time.grain`` buckets, in the temporal role's zone, no later than ``now``. The periods it
compares against come earlier, so they are complete too. Without ``time.end`` the window
reaches ``now``, and its last period is the one in progress. Plan checks the bounds it can
read and holds whatever it can't: a non-default calendar's buckets, or a ``where`` bound on
a date, which can cut a period short.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..ast import _floor_period, _parse_now, _shift_period, _time_spec_from_payload, every_filter
from ..errors import SemanticLayerError
from ..expressions import expr_to_dict
from ..visible_view import base_of
from .coverage import _dict_nodes, _time_block
from .time_reference import time_policy_context

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
    """Whether the draft reads a ``prior_period`` expression, its own or a metric's.

    A metric definition plan can't read counts as one: unknown means comparing.
    """

    recipes = {str(row.id): row for row in getattr(base_of(config), "metric_recipes", []) or []}
    seen: set[str] = set()
    pending: list[Any] = [query]
    while pending:
        for node in _dict_nodes(pending.pop()):
            if str(node.get("kind", "") or "").casefold() == "prior_period":
                return True
            for key in ("metric", "metric_recipe"):
                metric_id = node.get(key)
                if isinstance(metric_id, str) and metric_id in recipes and metric_id not in seen:
                    seen.add(metric_id)
                    try:
                        pending.append(expr_to_dict(recipes[metric_id].expression))
                    except Exception:  # noqa: BLE001 — an unreadable definition can't prove none
                        return True
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
    the complete-periods alternative keeps the rows from it. ``policy_context`` defaults to the
    plan's clock.
    """

    if not compares_periods(config, query):
        return None
    time = _time_block(query)
    grain = str(time.get("grain", "") or "").strip().lower()
    role = next((row for row in config.temporal_roles if row.id == time.get("temporal_role")), None)
    zone = str(getattr(role, "timezone", "") or "UTC")
    context = time_policy_context(policy_context)
    try:
        now = _local(_parse_now(context), zone)
    except (SemanticLayerError, ValueError):
        return None  # validation refuses an unreadable clock itself
    now_text = f"{now.isoformat(timespec='seconds')} ({zone})"
    calendar = str(time.get("calendar_id", "") or "default").strip()
    if calendar.lower() != "default":
        return _hold(
            f"The draft compares periods on calendar '{calendar}', and plan can't read where "
            f"that calendar's periods end, so it can't tell that each one is complete at now "
            f"({now_text}).",
            {"path": "time.calendar_id", "calendar_id": calendar, "now": now_text},
            "Compare periods on the default calendar, with query.time.end on a period end no "
            "later than now; plan holds a comparison on another calendar either way.",
        )
    if grain not in _GRAINS:
        return _hold(
            f"The draft compares periods, but its time.grain ('{grain}') doesn't say which "
            f"periods it returns, so plan can't tell that each one is complete at now "
            f"({now_text}).",
            {"path": "time.grain", "grain": grain, "now": now_text},
            "Set query.time.grain to the period the comparison reports, then validate.",
        )
    dated = sorted(
        {
            str(row.get("field"))
            for row in every_filter(query.get("where"))
            if isinstance(row, dict) and _is_dated(config, row.get("field"))
        }
    )
    try:
        spec = _time_spec_from_payload(time, policy_context=context, config=config)
        end = _local(_parse_bound(spec.end), zone) if spec is not None and spec.end else None
        reason = "the draft has no time.end, so its window runs to now"
    except (SemanticLayerError, ValueError, OverflowError):
        end, reason = None, "plan can't read its time.end"  # proves no boundary
    try:
        first = _local(_parse_bound(start), zone) if start else None
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
            _alternative(grain, complete_end, first, lead=f"Remove the filter on {dated[0]}. "),
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
        _alternative(grain, complete_end, first),
    )


def _hold(message: str, details: dict[str, Any], hint: str) -> dict[str, Any]:
    return {
        "code": _CODE,
        "message": message,
        "details": details,
        "recovery_hints": [{"kind": "compare_complete_periods", "message": hint}],
    }


def _alternative(
    grain: str, complete_end: datetime, first: datetime | None, *, lead: str = ""
) -> str:
    """The complete-periods alternative: every period up to the last one that has ended,
    from the question's dropped start (``first``) on."""

    last = _label(_previous(complete_end, grain), grain)
    bound = f"set query.time.end to {_iso(complete_end, grain)} (end-exclusive)"
    if first is not None and first >= complete_end:
        return (
            f"{lead}No {grain} from {_iso(first, grain)} on is complete yet. Compare complete "
            f"{grain}s through {last} instead: {bound}, then validate."
        )
    keep = f"; keep the rows dated {_iso(first, grain)} or later" if first else ""
    return f"{lead}Compare complete {grain}s through {last}: {bound}, then validate{keep}."


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
