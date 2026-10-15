"""Time grains, validity windows and external discontinuities."""

from __future__ import annotations

from typing import Any

from ..schema import MeasureExternalDiscontinuity, MeasureValidityWindow
from .row_values import _ensure_dict_list

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
    return [
        MeasureValidityWindow(
            from_=str(row.get("from", "") or ""),
            to=str(row.get("to", "") or ""),
            semantics=str(row.get("semantics", "") or ""),
        )
        for row in _ensure_dict_list(value)
    ]


def _parse_external_discontinuities(value: Any) -> list[MeasureExternalDiscontinuity]:
    out: list[MeasureExternalDiscontinuity] = []
    for row in _ensure_dict_list(value):
        magnitude = row.get("magnitude_estimate_pct")
        out.append(
            MeasureExternalDiscontinuity(
                from_=str(row.get("from", "") or ""),
                to=str(row.get("to", "") or ""),
                what=str(row.get("what", "") or ""),
                magnitude_estimate_pct=float(magnitude)
                if magnitude is not None and magnitude != ""
                else None,
            )
        )
    return out
