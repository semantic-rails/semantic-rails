"""Time grains, validity windows and external discontinuities."""

from __future__ import annotations

from typing import Any

from ..schema import MeasureExternalDiscontinuity, MeasureValidityWindow

_TIME_GRAIN_ORDER = ["transaction", "minute", "hour", "day", "week", "month", "quarter", "year"]


def _coarser_time_grains(grain: str) -> list[str]:
    text = str(grain or "").strip().lower()
    if text not in _TIME_GRAIN_ORDER:
        return [text] if text else []
    if text == "transaction":
        return list(_TIME_GRAIN_ORDER)
    if text == "week":
        return ["week"]  # weeks straddle month, quarter and year boundaries
    start = _TIME_GRAIN_ORDER.index(text)
    return _TIME_GRAIN_ORDER[start:]


def _parse_validity_windows(value: Any) -> list[MeasureValidityWindow]:
    rows = value if isinstance(value, list) else ([] if value is None else [value])
    out: list[MeasureValidityWindow] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            MeasureValidityWindow(
                from_=str(row.get("from", row.get("from_", "")) or ""),
                to=str(row.get("to", "") or ""),
                semantics=str(row.get("semantics", row.get("label", "")) or ""),
            )
        )
    return out


def _parse_external_discontinuities(value: Any) -> list[MeasureExternalDiscontinuity]:
    rows = value if isinstance(value, list) else ([] if value is None else [value])
    out: list[MeasureExternalDiscontinuity] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        magnitude = row.get("magnitude_estimate_pct")
        out.append(
            MeasureExternalDiscontinuity(
                from_=str(row.get("from", row.get("from_", "")) or ""),
                to=str(row.get("to", "") or ""),
                what=str(row.get("what", row.get("label", "")) or ""),
                magnitude_estimate_pct=float(magnitude)
                if magnitude is not None and magnitude != ""
                else None,
            )
        )
    return out
