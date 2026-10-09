"""Pattern: ``inline_period_shift``.

Detects YoY/MoM/WoW/QoQ comparisons. A side-by-side comparison of periods needs a read
widened by the lookback, which this release doesn't make, so the plan asks: its option is the
two complete periods as one series. A comparison whose period isn't over yet holds
(``PERIOD_COMPARISON_INCOMPLETE``).
"""

from __future__ import annotations

from typing import Any

from ...clock import parse_now
from .._base import (
    RuntimeCompositionDraft,
    _aggregation_from_text,
    _preferred_measure,
    _preferred_metric,
    _resolved,
    _tokens,
)
from ..call import QueryBuilder, Time
from ..generators import _target_focus_text
from ..groupings import _maybe_group_by, _time_spec
from ..qualifiers import _add_order, _target_measure_terms, _threshold_from_text
from ..time_phrases import _period_shift_grain
from ..time_reference import time_policy_context
from ._protocol import IntentPattern


def _is_implicit_active_threshold(threshold: tuple[str, Any], target_terms: list[str]) -> bool:
    """Return True when the threshold is the ``("gt", 0)`` fallback that
    ``_threshold_from_text`` emits for the words "active" / "activity",
    AND the resolved target measure already encodes that activeness
    (e.g. ``active_menu_count_eop``). In that case the "threshold" is
    really part of the measure name, not a qualification clause.
    """

    op, value = threshold
    if op != "gt" or value != 0:
        return False
    return "active" in target_terms


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    shift_grain = _period_shift_grain(text)
    if not shift_grain:
        return None
    target_terms = _target_measure_terms(text, terms)
    if not target_terms:
        return None
    governed_metric = _preferred_metric(runtime._config, target_terms)
    if governed_metric is not None and ("growth" in terms or "rate" in terms):
        return None
    # Avoid stealing the qualified rollup path — those carry a threshold.
    # ``_threshold_from_text`` falls back to an implicit ``("gt", 0)`` when
    # the intent contains "active"/"activity"; for snapshot measures like
    # ``active_menu_count_eop`` that's the measure name, not a
    # qualification threshold, so the period-shift pattern still applies.
    threshold = _threshold_from_text(text)
    if threshold is not None and not _is_implicit_active_threshold(threshold, target_terms):
        return None
    measure = _preferred_measure(runtime._config, target_terms, _tokens(_target_focus_text(text)))
    if measure is None or not measure.default_temporal_role:
        return None

    config = runtime._config
    role = measure.default_temporal_role
    asked = _time_spec(role, text)
    window = asked.window if asked is not None else {}
    grain = (asked.grain if asked is not None else "") or shift_grain
    # The two complete periods compared, as one series: the comparison itself, side by side,
    # needs a read widened by the lookback, which this release doesn't make.
    series = Time(temporal_role=role, grain=grain, last=(shift_grain, 2))
    builder = QueryBuilder(config).measure(measure.id, _aggregation_from_text(text, terms, measure))
    builder.time(series).groups(_maybe_group_by(config, text))
    query = _add_order(builder).build()
    option = {"call": query.to_call(), "reads": f"the last two complete {shift_grain}s, by {grain}"}
    now = parse_now((time_policy_context() or {}).get("now"))
    incomplete = bool(window) and Time(temporal_role=role).with_window(window).resolved(now).partial
    why = (
        {
            "code": "PERIOD_COMPARISON_INCOMPLETE",
            "message": (
                f"The question compares a {shift_grain} that isn't over yet with a complete one, "
                "so the two don't cover the same days."
            ),
            "details": {"window": window, "options": [option]},
            "recovery_hints": [
                {
                    "kind": "compare_complete_periods",
                    "message": f"Compare complete {shift_grain}s: send the option's call.",
                }
            ],
        }
        if incomplete
        else {
            "code": "PERIOD_COMPARISON",
            "message": (
                f"A {shift_grain}-over-{shift_grain} comparison side by side isn't answered in "
                f"this release; the {shift_grain}s compared can be read as one series."
            ),
            "details": {
                "clarification": {"question": f"Read the last two {shift_grain}s as a series?"},
                "options": [option],
            },
            "recovery_hints": [{"kind": "read_the_series", "message": "Send the option's call."}],
        }
    )
    return RuntimeCompositionDraft(
        query=query,
        resolved=[_resolved(measure)],
        rationale=[f"read a {shift_grain}-over-{shift_grain} comparison as its two periods"],
        interpreted_intent={
            "pattern": "inline_period_shift",
            "source": "plan",
            "measure": measure.id,
            "shift_grain": shift_grain,
        },
        blocked_reason=why,
    )


PATTERN = IntentPattern(name="inline_period_shift", match=_match)
