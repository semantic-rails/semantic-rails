"""Verify row presence promised by an unfiltered, bounded filled series."""

from __future__ import annotations

import re
from datetime import date, datetime
from datetime import time as clock_time
from typing import Any
from zoneinfo import ZoneInfo

from ..ast import _floor_period, _shift_period
from ..errors import SemanticLayerError

# ``datetime`` keeps six fractional digits and silently drops the rest.
_FRACTION = re.compile(r"[.,](\d+)")


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


def _exact_bound(value: Any, zone: str) -> datetime | None:
    if isinstance(value, str) and any(d[6:].strip("0") for d in _FRACTION.findall(value)):
        return None
    return _local_datetime(value, zone)


def _period_date(value: Any, zone: str) -> date | None:
    stamp = _local_datetime(value, zone)
    return stamp.date() if stamp is not None else None


def enforce_fill_contract(
    query: dict[str, Any], rows: list[dict[str, Any]], *, truncated: bool, zone: str
) -> None:
    """Return only when the period keys are exactly the computed buckets.

    NULL metric values still constitute present rows. Bounds that do not parse exactly
    and keys outside the computed buckets (another bucket anchor, an invalid key) refuse.
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
    start = _exact_bound(time["start"], zone)
    end = _exact_bound(time["end"], zone)
    if start is None or end is None:
        raise SemanticLayerError(
            "FILL_INCOMPLETE",
            "A filled bounded series could not be proved complete; send start and end as "
            "ISO dates or datetimes with at most six fractional-second digits",
            details={
                "calendar": "default",
                "reason": "unverifiable",
                "bounds": [name for name, at in (("start", start), ("end", end)) if at is None],
            },
        )
    expected = set()
    bucket = _floor_period(start.date(), time["grain"])
    # Buckets intersecting [start, end); an empty window intersects none.
    while start < end and datetime.combine(bucket, clock_time.min) < end:
        expected.add(bucket)
        bucket = _shift_period(bucket, time["grain"], 1)
    key = f"{time['temporal_role']}__{time['grain']}"
    returned = {_period_date(row.get(key), zone) for row in rows}
    if returned == expected:
        return
    details: dict[str, Any] = {
        "calendar": "default",
        "missing_periods": [period.isoformat() for period in sorted(expected - returned)],
        "hint": f"extend the authored calendar through {time['end']}",
    }
    message = "A filled bounded series returned incomplete periods; " + details["hint"]
    if not returned <= expected:
        details.update(
            reason="unverifiable",
            expected_periods=len(expected),
            returned_periods=len(returned - {None}),
            hint=f"the returned periods are not the computed {time['grain']} buckets",
        )
        message = "A filled bounded series could not be proved complete; " + details["hint"]
    raise SemanticLayerError("FILL_INCOMPLETE", message, details=details)
