"""Pattern: ``inline_period_shift``.

Detects YoY/MoM/WoW/QoQ comparisons and emits a 2-column IR using the
``prior_period`` shorthand from the Phase 4 schema.
"""

from __future__ import annotations

from typing import Any

from .._base import (
    RuntimeCompositionDraft,
    _aggregation_from_text,
    _named_metric,
    _preferred_measure,
    _preferred_metric,
    _resolved,
    _semantic_token,
    _tokens,
)
from ..generators import _target_focus_text
from ..groupings import _maybe_group_by, _time_spec
from ..qualifiers import _add_order, _target_measure_terms, _threshold_from_text
from ..time_phrases import _period_shift_grain
from ._protocol import IntentPattern
from .metric_by_dimension_rollup import _governed_target


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
    named = _named_metric(runtime._config, text)
    target_terms = _target_measure_terms(text, terms)
    if not target_terms and named is None:
        return None
    governed_metric = _preferred_metric(runtime._config, target_terms)
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
    measure = _preferred_measure(runtime._config, target_terms, _tokens(_target_focus_text(text)))
    subject = named[0] if named else measure
    role = str(
        getattr(subject, "temporal_role", "") or getattr(subject, "default_temporal_role", "")
    )
    if subject is None or not role:
        return None

    time_spec = _time_spec(role, text)
    # Preserve the authored window. Validation reports a lookback conflict; the common
    # planner may retry a bounded start only while reporting TIME_WINDOW_START_DROPPED.
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

    expression = (
        {"metric": subject.id}
        if named
        else {
            "measure": subject.id,
            "aggregation": _aggregation_from_text(text, terms, subject),
        }
    )
    if (
        not named
        and (
            governed := _governed_target(
                runtime._config,
                text,
                {
                    "select": [{"expression": expression}],
                    "group_by": _maybe_group_by(runtime._config, text),
                },
            )
        )
        is not None
    ):
        subject, expression = governed, {"metric": governed.id}
    base_alias = _semantic_token(subject.id, fallback="measure")
    prior_alias = f"{base_alias}_prior_{shift_grain}"
    prior = (
        {"kind": "prior_period", "input": expression, "offset": {"unit": shift_grain, "value": 1}}
        if "metric" in expression
        else {"kind": "prior_period", **expression, "offset": -1, "grain": shift_grain}
    )

    query: dict[str, Any] = {
        "version": 1,
        "select": [
            {
                "as": base_alias,
                "expression": expression,
            },
            {
                "as": prior_alias,
                "expression": prior,
            },
        ],
        "time": time_spec,
    }
    group_by = _maybe_group_by(runtime._config, text)
    if group_by:
        query["group_by"] = group_by
    _add_order(query)

    return RuntimeCompositionDraft(
        query=query,
        resolved=[_resolved(subject)],
        rationale=[
            f"composed inline {shift_grain}-over-{shift_grain} comparison via the prior_period shorthand",
        ],
        interpreted_intent={
            "pattern": "inline_period_shift",
            "source": "plan",
            "measure": subject.id,
            "shift_grain": shift_grain,
            "shift_offset": -1,
            "current_alias": base_alias,
            "prior_alias": prior_alias,
        },
    )


PATTERN = IntentPattern(name="inline_period_shift", match=_match)
