"""Readiness: a period comparison never puts an incomplete period beside a complete one.

A draft compares periods when its call, read as execution reads it, carries a ``prior_period``
expression anywhere (a select or a ``having``, inside arithmetic or not), or reads a metric whose
definition carries one, through any chain of metrics. A comparison takes no window but an end
(validation refuses one beside ``last``, ``this`` or a ``start``), so it is ready only when
``time.end`` falls on a boundary of the ``time.grain`` buckets, in the clock's zone, no later
than ``now``. The periods it compares against come earlier, so they are complete too. Without
``time.end`` the window runs to ``now``, and its last period is the one in progress. The check
reads the bounds it can and holds whatever it can't: a draft it can't read, a ``where`` bound on
a date, which can cut a period short, or a grain or clock it can't read.

``plan``, the granted-metric plan and a matched package example all call it, on the draft's
engine form (``Runtime.engine_payload``, which carries ``now``), before offering
``ready_for: execute``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from typing import Any

from .ast import every_filter, normalize_query
from .clock import UNITS, floor_period, local_now, parse_now, shift_period
from .errors import SemanticLayerError
from .expressions import MetricRecipeRefExpr, OffsetWindowExpr
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
    alternative keeps the rows from it.
    """

    if not compares_periods(config, query):
        return None
    raw_time = query.get("time")
    time: Mapping[str, Any] = raw_time if isinstance(raw_time, Mapping) else {}
    grain = str(time.get("grain", "") or "").strip().lower()
    role = next((row for row in config.temporal_roles if row.id == time.get("temporal_role")), None)
    zone = str(getattr(role, "timezone", "") or "UTC")
    try:
        instant = parse_now((query.get("policy_context") or {}).get("now"))
        now = local_now(instant, zone)
    except (SemanticLayerError, ValueError, OverflowError):
        return _hold(
            "The draft compares periods, but the request's now can't be read, so plan can't "
            "tell that each period is complete.",
            {"path": "now"},
            "Send now as an ISO date or date-time, then plan again.",
        )
    now_text = f"{now.isoformat(timespec='seconds')} {zone}"
    if grain not in UNITS:
        return _hold(
            f"The draft compares periods, but its time.grain ('{grain}') doesn't say which "
            f"periods it returns, so plan can't tell that each one is complete at now "
            f"({now_text}).",
            {"path": "time.grain", "grain": grain, "now": now_text},
            "Set time.grain to the period the comparison reports, then plan again.",
        )
    dated = sorted(
        {
            str(row.get("field"))
            for row in every_filter(query.get("where"))
            if isinstance(row, Mapping) and _is_dated(config, row.get("field"))
        }
    )
    try:
        end = _local(_parse_bound(time["end"]), zone) if time.get("end") is not None else None
        reason = "the draft has no time.end, so its window runs to now"
    except (ValueError, OverflowError):
        end, reason = None, "plan can't read its time.end"  # proves no boundary
    try:
        first = _local(_parse_bound(start), zone) if start else None
    except (ValueError, OverflowError):
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
            _alternative(grain, complete_end, first),
            _option(call, grain, complete_end),
        )
    if end is None or end > now:
        incomplete = complete_end = current
        if end is not None:
            reason = f"the window runs to {_iso(end)}"
    else:
        incomplete = complete_end = _floor(end, grain)
        reason = f"time.end {_iso(end)} cuts it short"
    return _hold(
        f"The draft compares each {grain} with an earlier period, but {_label(incomplete, grain)} "
        f"is not complete at now ({now_text}): {reason}. It would sit beside a complete period "
        "as if it were one.",
        {
            "path": "time.end",
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
        _option(call, grain, complete_end),
    )


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


def _is_dated(config: Any, field: Any) -> bool:
    dimension = next((row for row in config.dimensions if row.id == field), None)
    return str(getattr(dimension, "data_type", "") or "").lower() in {"date", "timestamp"}


def _parse_bound(value: Any) -> date | datetime:
    if isinstance(value, (date, datetime)):
        return value
    return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))


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
