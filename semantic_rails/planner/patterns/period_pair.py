"""One comparison phrase, exactly two completed adjacent flow periods."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from ...ast import _relative_range_bounds, _time_spec_from_payload
from ...errors import SemanticLayerError
from ...expressions import collect_object_references, expr_to_dict
from .._base import RuntimeCompositionDraft, _object_by_id, _resolved, _semantic_token
from ..time_phrases import (
    _calendar_windows,
    _relative_window,
    _time_cues,
)
from ..time_reference import time_policy_context, time_timezone
from ._protocol import IntentPattern

_UNIT = r"day|week|month|quarter|year"
_COMPARISON = r"compared\s+(?:with|to)|vs\.?|versus|against|change\s+from|up\s+or\s+down"
_PAIR = re.compile(
    rf"\b(?P<first>(?:last|this)\s+(?P<unit>{_UNIT})|yesterday)\s*,?\s+"
    rf"(?:(?:{_COMPARISON})\s+(?:the\s+)?"
    rf"(?:(?P<modifier>previous|prior|last)\s+(?P<leading>{_UNIT})|"
    rf"(?P<trailing>{_UNIT})\s+(?:before|prior))|(?P<implicit>up\s+or\s+down))"
    r"[?.!]*\s*$",
    re.IGNORECASE,
)
_COMPARISON_RE = re.compile(rf"\b(?:{_COMPARISON})\b", re.IGNORECASE)


@dataclass(frozen=True)
class PeriodPair:
    unit: str
    span: tuple[int, int]

    @property
    def bounds(self) -> dict[str, Any]:
        return {"range": {"last": {"unit": self.unit, "value": 2}}}


def completed_period_pair(
    text: str,
    *,
    runtime: Any = None,
    query: dict[str, Any] | None = None,
    today: date | None = None,
) -> PeriodPair | None:
    """Read the whole pair; with a draft, prove its subject, shape and resolved bounds.

    The window resolver uses the grammar reading without a draft. Readiness callers must
    supply both the runtime and draft; a phrase alone never proves an answer.
    """
    match = _PAIR.search(text)
    if match is None:
        return None
    if match["first"].lower().startswith("this ") or (match["modifier"] or "").lower() == "last":
        return None
    unit = (match["unit"] or "day").lower()
    if (match["leading"] or match["trailing"] or unit).lower() != unit:
        return None
    # Another comparison outside the consumed phrase cannot be answered by this pair.
    if _COMPARISON_RE.search(text[: match.start()]):
        # "Were closures up or down last week compared with ..." asks the same pair.
        prefix = re.sub(r"\bup\s+or\s+down\b", "", text[: match.start()], flags=re.IGNORECASE)
        if _COMPARISON_RE.search(prefix):
            return None
    prefix = text[: match.start()].lower()
    accepted, rejected = _calendar_windows(prefix)
    if (
        accepted
        or rejected
        or _relative_window(prefix, today or date.today())
        or _time_cues(prefix)
    ):
        return None
    # Punctuation belongs to the question, not the consumed time phrase.
    pair = PeriodPair(unit, (match.start(), match.start() + len(match.group().rstrip(" ?.!"))))
    if query is None:
        return pair
    time = query.get("time") or {}
    values = query.get("select") or []
    if not isinstance(time, dict) or not isinstance(values, list):
        return None
    if (
        runtime is None
        or len(values) != 1
        or not isinstance(values[0], dict)
        or query.get("group_by")
        or query.get("limit") is not None
        or time.get("grain") != unit
        or time.get("range") != pair.bounds["range"]
        or query.get("order_by") != [{"field": "time", "direction": "ASC"}]
    ):
        return None
    expression = values[0].get("expression") or {}
    if not isinstance(expression, dict) or not _flow_subject(runtime._config, expression):
        return None
    context = time_policy_context(query.get("policy_context"))
    if today is not None:
        context = {"now": today}
    try:
        resolved = _time_spec_from_payload(time, policy_context=context, config=runtime._config)
        expected = _relative_range_bounds(
            pair.bounds["range"], policy_context=context, timezone=time_timezone()
        )
        # Compare actual role/calendar-aware bounds with the two completed periods in the
        # planning zone. A partial period, extra bound or different clock cannot qualify.
        if resolved is None or (resolved.start, resolved.end) != (
            expected["start"],
            expected["end"],
        ):
            return None
    except (SemanticLayerError, ValueError, TypeError, OverflowError):
        return None
    return pair


def _flow_subject(config: Any, expression: dict[str, Any]) -> bool:
    if set(expression) - {"metric", "measure", "aggregation"}:
        return False
    subject = expression.get("metric") or expression.get("measure")
    if not subject or bool(expression.get("metric")) == bool(expression.get("measure")):
        return False
    try:
        references = set(collect_object_references(expression, config))
        pending = list(references)
        while pending:
            reference = pending.pop()
            if (metric := _object_by_id(config.metric_recipes, reference)) is not None:
                children = set(collect_object_references(expr_to_dict(metric.expression), config))
                pending.extend(children - references)
                references |= children
    except SemanticLayerError:
        return False
    measures = [row for row in config.measures if row.id in references]
    return bool(measures) and all(
        row.accumulation.kind in {"", "flow", "event", "population"}
        and row.measure_class != "semi_additive"
        for row in measures
    )


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    from .._base import (  # noqa: WPS433
        _aggregation_from_text,
        _named_metric,
        _preferred_measure,
        _preferred_metric,
        _tokens,
    )
    from ..generators import _target_focus_text  # noqa: WPS433
    from ..groupings import _maybe_group_by  # noqa: WPS433
    from .metric_by_dimension_rollup import _governed_target  # noqa: WPS433

    if not _COMPARISON_RE.search(text):
        return None
    pair = completed_period_pair(text)
    # Unknown comparison syntax retains the existing patterns and guards.
    if pair is None and _PAIR.search(text) is None:
        return None
    focus = _target_focus_text(text[: pair.span[0]] if pair else text)
    named = _named_metric(runtime._config, focus)
    subject = (
        named[0]
        if named
        else (
            _preferred_measure(runtime._config, terms, _tokens(focus))
            or _preferred_metric(runtime._config, _tokens(focus))
        )
    )
    if subject is None:
        return None
    role = str(
        getattr(subject, "temporal_role", "") or getattr(subject, "default_temporal_role", "")
    )
    expression = (
        {"metric": subject.id}
        if _object_by_id(runtime._config.metric_recipes, subject.id) is not None
        else {
            "measure": subject.id,
            "aggregation": _aggregation_from_text(text, terms, subject),
        }
    )
    query: dict[str, Any] = {
        "version": 1,
        "select": [{"as": _semantic_token(subject.id), "expression": expression}],
        "time": {
            "temporal_role": role,
            "grain": pair.unit if pair else "week",
            **(pair.bounds if pair else {}),
            "fill": True,
        },
        "order_by": [{"field": "time", "direction": "ASC"}],
    }
    if (governed := _governed_target(runtime._config, focus, query)) is not None:
        subject = governed
        query["select"] = [
            {"as": _semantic_token(subject.id), "expression": {"metric": subject.id}}
        ]
    if grouped := _maybe_group_by(runtime._config, text):
        query["group_by"] = grouped
    matched = completed_period_pair(text, runtime=runtime, query=query)
    # Preserve the original draft; a hold never synthesizes another comparison.
    blocked: dict[str, Any] = (
        {}
        if matched
        else {
            "code": "PLAN_INTENT_COVERAGE_GAP",
            "message": (
                "Plan reads two completed adjacent flow periods: 'last week compared with "
                "the week before', 'last month vs the previous month', or 'yesterday vs the "
                "day before'. Current partial periods, stocks, named windows and mixed units "
                "need clarification."
            ),
            "recovery_hints": [
                {
                    "kind": "clarify_comparison",
                    "message": "Name one completed period and the same unit immediately before it.",
                }
            ],
        }
    )
    return RuntimeCompositionDraft(
        query=query,
        resolved=[_resolved(subject)],
        rationale=["compare two dated completed periods"],
        interpreted_intent={
            "pattern": "period_pair",
            "source": "plan",
            "span": list(pair.span) if pair else [],
        },
        blocked_reason=blocked,
    )


PATTERN = IntentPattern(name="period_pair", match=_match)
