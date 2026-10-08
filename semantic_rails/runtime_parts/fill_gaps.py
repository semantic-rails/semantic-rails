"""Verify row presence promised by an unfiltered, bounded filled series."""

from __future__ import annotations

from datetime import date, datetime
from datetime import time as clock_time
from typing import Any
from zoneinfo import ZoneInfo

from ..ast import _floor_period, _shift_period
from ..errors import SemanticLayerError


def _local_datetime(value: Any, zone: str) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(ZoneInfo(zone or "UTC"))
        return value.replace(tzinfo=None)
    return datetime.combine(value, clock_time.min) if isinstance(value, date) else None


def _period_date(value: Any, zone: str) -> date | None:
    stamp = _local_datetime(value, zone)
    return stamp.date() if stamp is not None else None


def enforce_fill_contract(
    query: dict[str, Any], rows: list[dict[str, Any]], *, truncated: bool, zone: str
) -> None:
    """Refuse incomplete fills; NULL metric values still constitute present rows.

    A different authored bucket convention uses distinct-period counts instead of
    inventing Gregorian labels for missing authored periods.
    """
    time = query.get("time") or {}
    if (
        time.get("fill") is not True
        or not all(time.get(key) for key in ("start", "end", "grain", "temporal_role"))
        or str(time.get("calendar_id") or "default").strip().lower() != "default"
        or query.get("group_by")
        or query.get("metric_filters")
        or query.get("limit") is not None
        or truncated
    ):
        return
    start = _period_date(time["start"], zone)
    end = _local_datetime(time["end"], zone)
    if start is None or end is None:
        return  # Query normalization owns validity of the bounds.
    expected = set()
    bucket = _floor_period(start, time["grain"])
    while datetime.combine(bucket, clock_time.min) < end:
        expected.add(bucket)
        bucket = _shift_period(bucket, time["grain"], 1)
    key = f"{time['temporal_role']}__{time['grain']}"
    returned = {_period_date(row.get(key), zone) for row in rows}
    missing = expected - returned
    details: dict[str, Any] = {
        "calendar": "default",
        "missing_periods": [period.isoformat() for period in sorted(missing)],
        "hint": f"extend the authored calendar through {time['end']}",
    }
    if not returned <= expected:
        details.update(expected_periods=len(expected), returned_periods=len(returned - {None}))
        # Invalid keys cannot establish row presence; alternative calendar labels can.
        if None not in returned and len(returned) == len(expected):
            return
    elif not missing:
        return
    raise SemanticLayerError(
        "FILL_INCOMPLETE",
        "A filled bounded series returned incomplete periods; " + details["hint"],
        details=details,
    )
