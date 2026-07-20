"""Pattern: ``inline_period_shift``.

Detects YoY/MoM/WoW/QoQ comparisons and emits a 2-column IR using the
``prior_period`` shorthand from the Phase 4 schema.
"""

from __future__ import annotations

from typing import Any

from .._base import (
    RuntimeCompositionDraft,
    _add_order,
    _aggregation_from_text,
    _maybe_group_by,
    _period_shift_grain,
    _preferred_measure,
    _preferred_metric,
    _resolved,
    _semantic_token,
    _target_measure_terms,
    _threshold_from_text,
    _time_spec,
)
from ._protocol import IntentPattern


def _is_implicit_active_threshold(threshold: tuple[str, Any], target_terms: list[str]) -> bool:
    """Return True when the threshold is the ``(">", 0)`` fallback that
    ``_threshold_from_text`` emits for the words "active" / "activity",
    AND the resolved target measure already encodes that activeness
    (e.g. ``active_menu_count_eop``). In that case the "threshold" is
    really part of the measure name, not a qualification clause.
    """

    op, value = threshold
    if op != ">" or value != 0:
        return False
    return "active" in target_terms


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    shift_grain = _period_shift_grain(text)
    if not shift_grain:
        return None
    target_terms = _target_measure_terms(text, terms)
    if not target_terms:
        return None
    governed_metric = _preferred_metric(runtime.config, target_terms)
    if governed_metric is not None and ("growth" in terms or "rate" in terms):
        return None
    # Avoid stealing the qualified rollup path — those carry a threshold.
    # ``_threshold_from_text`` falls back to an implicit ``(">", 0)`` when
    # the intent contains "active"/"activity"; for snapshot measures like
    # ``active_menu_count_eop`` that's the measure name, not a
    # qualification threshold, so the period-shift pattern still applies.
    threshold = _threshold_from_text(text)
    if threshold is not None and not _is_implicit_active_threshold(threshold, target_terms):
        return None
    measure = _preferred_measure(runtime.config, target_terms)
    if measure is None or not measure.default_temporal_role:
        return None

    time_spec = _time_spec(measure.default_temporal_role, text)
    # The compiler rejects bounded starts on inline window expressions
    # because they can truncate the rows required for the lookback. Keep
    # the upper bound from phrases like "this month", but let the
    # prior_period expression own its lookback window. A relative range
    # ("last 6 months") expands to a bounded start too, so it is dropped
    # for the same reason.
    time_spec.pop("start", None)
    time_spec.pop("range", None)
    requested_grain = str(time_spec.get("grain", "") or "")
    if not requested_grain or requested_grain == shift_grain:
        finer_map = {
            "year": "month",
            "quarter": "month",
            "month": "month",
            "week": "day",
            "day": "day",
        }
        time_spec["grain"] = finer_map.get(shift_grain, "month")

    base_alias = _semantic_token(measure.id, fallback="measure")
    base_aggregation = _aggregation_from_text(text, terms, measure)
    prior_alias = f"{base_alias}_prior_{shift_grain}"

    query: dict[str, Any] = {
        "version": 2,
        "select": [
            {
                "as": base_alias,
                "expression": {
                    "measure": measure.id,
                    "aggregation": base_aggregation,
                },
            },
            {
                "as": prior_alias,
                "expression": {
                    "kind": "prior_period",
                    "measure": measure.id,
                    "offset": -1,
                    "grain": shift_grain,
                    "aggregation": base_aggregation,
                },
            },
        ],
        "time": time_spec,
    }
    group_by = _maybe_group_by(runtime.config, text)
    if group_by:
        query["group_by"] = group_by
    _add_order(query)

    return RuntimeCompositionDraft(
        query=query,
        resolved=[_resolved(measure)],
        rationale=[
            f"composed inline {shift_grain}-over-{shift_grain} comparison via the prior_period shorthand",
        ],
        interpreted_intent={
            "pattern": "inline_period_shift",
            "source": "plan",
            "measure": measure.id,
            "shift_grain": shift_grain,
            "shift_offset": -1,
            "current_alias": base_alias,
            "prior_alias": prior_alias,
        },
    )


PATTERN = IntentPattern(name="inline_period_shift", match=_match)
