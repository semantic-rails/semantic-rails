"""The query's clock: ``now`` and the time windows it anchors.

``now`` is a request argument (default: the server clock, read only here). A relative window
(``last``, ``this``) resolves once, in the clock's time zone, to half-open ``[start, end)``
bounds that the compiler reads; the answer reports the same window back.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .errors import SemanticLayerError

UNITS: tuple[str, ...] = ("day", "week", "month", "quarter", "year")
# Weeks start on Monday (ISO 8601), the same week every dialect's date_trunc uses.
WEEK_START = "monday"


def current_time() -> datetime:
    """The server clock: the only read of the current time in the engine."""
    return datetime.now(UTC)


def parse_now(raw: Any, *, path: str = "now") -> datetime:
    """An aware instant for ``now``: an ISO date-time, or a date meaning its first instant (UTC)."""
    if raw is None or raw == "":
        return current_time()
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=UTC)
    text = str(raw).strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            day = date.fromisoformat(text)
        except ValueError as exc:
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"{path} must be an ISO date or date-time",
                details={"path": path, "received_value": text},
            ) from exc
        return datetime(day.year, day.month, day.day, tzinfo=UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def format_now(now: datetime) -> str:
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _zone(name: str) -> Any:
    return UTC if not name or name == "UTC" else ZoneInfo(name)


def local_now(now: datetime, timezone: str) -> datetime:
    """``now`` as wall time in ``timezone`` (naive), the zone the clock's bounds are read in."""
    return now.astimezone(_zone(timezone)).replace(tzinfo=None)


def _add_months(value: date, months: int) -> date:
    index = value.month - 1 + months
    year = value.year + index // 12
    month = index % 12 + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def floor_period(day: date, unit: str) -> date:
    if unit == "day":
        return day
    if unit == "week":
        return day - timedelta(days=day.weekday())
    if unit == "month":
        return date(day.year, day.month, 1)
    if unit == "quarter":
        return date(day.year, ((day.month - 1) // 3) * 3 + 1, 1)
    if unit == "year":
        return date(day.year, 1, 1)
    raise SemanticLayerError(
        "INVALID_QUERY",
        f"Unsupported time unit '{unit}'",
        details={"supported_units": list(UNITS)},
    )


def shift_period(day: date, unit: str, periods: int) -> date:
    if unit == "day":
        return day + timedelta(days=periods)
    if unit == "week":
        return day + timedelta(weeks=periods)
    months = {"month": 1, "quarter": 3, "year": 12}.get(unit)
    if months is None:
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"Unsupported time unit '{unit}'",
            details={"supported_units": list(UNITS)},
        )
    return _add_months(day, periods * months)


def period_end(start: date, grain: str) -> date:
    """The first day after the period that starts on ``start`` (exclusive end)."""
    return shift_period(floor_period(start, grain), grain, 1)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def period_is_partial(period: Any, grain: str, now: datetime, timezone: str = "UTC") -> bool:
    """Whether the period starting at ``period`` ends after ``now`` (so it is not complete)."""
    start = _as_date(period)
    if start is None or grain not in UNITS:
        return False
    end = period_end(start, grain)
    return datetime(end.year, end.month, end.day) > local_now(now, timezone)


@dataclass(frozen=True)
class Window:
    """A resolved window: the public spelling, its ``[start, end)`` bounds and its kind."""

    spec: dict[str, Any]
    start: str | None
    end: str | None
    # A named window (``last``, ``this``, ``start`` + ``end``) always answers with its rows.
    named: bool
    # Whether the window reaches past ``now`` (its last period is incomplete).
    partial: bool


def resolve_window(spec: dict[str, Any], *, now: datetime, timezone: str = "UTC") -> Window:
    """Resolve a time block's window keys (``last`` | ``this`` | ``start``/``end``)."""
    today = local_now(now, timezone)
    if "last" in spec:
        last = spec["last"]
        unit, value = last["unit"], int(last["value"])
        end = floor_period(today.date(), unit)
        start = shift_period(end, unit, -value)
        return Window(
            spec={"last": {"unit": unit, "value": value}},
            start=start.isoformat(),
            end=end.isoformat(),
            named=True,
            partial=False,
        )
    if "this" in spec:
        unit = spec["this"]
        start = floor_period(today.date(), unit)
        return Window(
            spec={"this": unit},
            start=start.isoformat(),
            end=today.isoformat(timespec="seconds"),
            named=True,
            partial=True,
        )
    start_value, end_value = spec.get("start"), spec.get("end")
    end_day = _as_date(end_value)
    partial = end_value is None and start_value is not None
    if end_value is not None and end_day is not None:
        end_moment = _end_moment(end_value)
        partial = end_moment is not None and end_moment > today
    window_spec = {key: spec[key] for key in ("start", "end") if spec.get(key) is not None}
    return Window(
        spec=window_spec,
        start=None if start_value is None else str(start_value),
        end=None if end_value is None else str(end_value),
        named=start_value is not None and end_value is not None,
        partial=partial,
    )


def engine_window(time: Mapping[str, Any], *, now: datetime, timezone: str = "UTC") -> Window:
    """The window an engine time block resolved, with its relative spelling kept."""
    if time.get("last_unit"):
        spec: dict[str, Any] = {
            "last": {"unit": time["last_unit"], "value": int(time.get("last_value") or 1)}
        }
    elif time.get("this"):
        spec = {"this": time["this"]}
    else:
        spec = {key: time[key] for key in ("start", "end") if time.get(key) is not None}
    return resolve_window(spec, now=now, timezone=timezone)


def local_date(instant: datetime, timezone: str = "UTC") -> str:
    """The calendar date of ``instant`` (naive means UTC) in ``timezone``."""
    aware = instant if instant.tzinfo is not None else instant.replace(tzinfo=UTC)
    return aware.astimezone(_zone(timezone)).date().isoformat()


def _end_moment(value: Any) -> datetime | None:
    text = str(value).strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.replace(tzinfo=None) if moment.tzinfo is None else moment.replace(tzinfo=None)


def describe_window(window: Window, *, now: datetime, timezone: str = "UTC") -> str:
    """'October so far (8 of 31 days)' for a ``this`` window; '' otherwise."""
    if "this" not in window.spec or window.start is None:
        return ""
    unit = window.spec["this"]
    start = date.fromisoformat(window.start)
    end = period_end(start, unit)
    today = local_now(now, timezone).date()
    elapsed = (today - start).days + 1
    total = (end - start).days
    names = {
        "day": start.isoformat(),
        "week": f"the week of {start.isoformat()}",
        "month": start.strftime("%B %Y"),
        "quarter": f"Q{(start.month - 1) // 3 + 1} {start.year}",
        "year": str(start.year),
    }
    if unit == "day":
        return f"{names[unit]} so far"
    return f"{names[unit]} so far ({elapsed} of {total} days)"


def range_bounds(range_payload: Any, *, now: Any, timezone: str = "UTC") -> dict[str, str]:
    """``{start, end}`` for a planner draft's relative range ``{"last": {unit, value}}``."""
    last = range_payload.get("last") if isinstance(range_payload, dict) else None
    if not isinstance(last, dict) or last.get("unit") not in UNITS:
        raise SemanticLayerError("INVALID_QUERY", "A relative range is {last: {unit, value}}")
    try:
        value = int(last.get("value"))  # type: ignore[arg-type]  # None raises TypeError
    except (TypeError, ValueError) as exc:
        raise SemanticLayerError("INVALID_QUERY", "last.value must be a positive integer") from exc
    if value <= 0:
        raise SemanticLayerError("INVALID_QUERY", "last.value must be a positive integer")
    window = resolve_window(
        {"last": {"unit": last["unit"], "value": value}},
        now=parse_now(now),
        timezone=timezone,
    )
    return {"start": str(window.start), "end": str(window.end)}
