"""Pattern: ``qualified_metric_rollup``.

Detects intents that ask for a metric rolled up to one entity grain while filtering by a
count/threshold predicate evaluated at another ("top stores by revenue with at least 4
distinct customers"). The qualification is an entity filter on the entity's aggregates,
taken ``over`` all time ("lifetime", "ever"), the query window, or each period ("that
month"); a question that doesn't say which asks (``ENTITY_FILTER_PERIOD_UNSTATED``), with one
ready-to-send call per reading. A share of the population ("top decile of customers") is a
percentile threshold on a scoped aggregate's predicate.
"""

from __future__ import annotations

import re
from typing import Any

from ...config_parts.measure_governance import _reachable_entities
from .._base import (
    RuntimeCompositionDraft,
    _aggregation_from_text,
    _object_by_id,
    _preferred_measure,
    _preferred_metric,
    _resolved,
    _score,
    _tied_top,
    _tokens,
)
from ..call import Call, Condition, EntityFilter, QueryBuilder
from ..generators import _target_focus_text
from ..groupings import _maybe_group_by, _time_spec
from ..qualifiers import (
    _QUALIFICATION_STOP_TOKENS,
    _add_order,
    _product_filter,
    _qualification_phrase,
    _qualification_phrase_token_groups,
    _qualifying_entity,
    _target_measure_terms,
    _threshold_from_text,
    _top_n_intent,
)
from ._protocol import IntentPattern


def _predicate_metric_terms(text: str, terms: set[str], target_terms: list[str]) -> list[list[str]]:
    """Return predicate-metric term groups for ``qualified_metric_rollup``."""
    phrase_groups = _qualification_phrase_token_groups(text, target_terms)
    if phrase_groups:
        return phrase_groups
    predicate_terms: list[list[str]] = []
    for channel in ("sms", "push", "email"):
        if channel in terms:
            predicate_terms.append([channel])
    if predicate_terms:
        return predicate_terms
    if "order" in terms and "order" not in (target_terms or []):
        predicate_terms.append(["order"])
    if "session" in terms and "session" not in (target_terms or []):
        predicate_terms.append(["session"])
    if predicate_terms:
        return predicate_terms
    if "message" in terms and "message" not in (target_terms or []):
        return [["message"]]
    return []


def _predicate_grain(text: str) -> str:
    """The period a qualification is counted in, when the question says ("that month")."""
    lowered = str(text or "").lower()
    for candidate in ("day", "week", "month", "quarter", "year"):
        if any(
            cue.format(candidate) in lowered
            for cue in ("that {}", "same {}", "in the {}", "in that {}", "per {}")
        ):
            return candidate
    return ""


# "lifetime", "ever", "of all time": the entity's aggregate over every row it has.
_ALL_TIME_RE = re.compile(r"\b(?:lifetime|ever|all[\s-]time)\b")


def _over(text: str, call: Call) -> str:
    """How the question takes the qualifying aggregate: ``all_time``, ``period`` (its own
    output grain), or "" when it doesn't say."""
    if _ALL_TIME_RE.search(str(text or "").lower()):
        return "all_time"
    grain = _predicate_grain(text)
    if grain and call.time is not None and call.time.grain == grain:
        return "period"
    return ""


def _qualified(
    config: Any,
    builder_for: Any,
    over: str,
    entity: Any,
    predicates: list[Any],
    op: str,
    value: Any,
) -> Call:
    """The draft with the qualification taken ``over`` the given rows."""
    builder = builder_for()
    if isinstance(value, dict):
        # A population share is a percentile threshold, which only a predicate takes.
        [item] = builder.build().select
        measure = item.ref
        builder.selects([])
        builder.select(
            {
                "kind": "scoped_aggregate",
                "measure": measure,
                "aggregation": item.aggregation
                or next(row.default_aggregation for row in config.measures if row.id == measure),
                "predicates": [
                    {
                        "entity": entity.id,
                        "input": target.id,
                        "op": op,
                        "value": value,
                        "over": over,
                    }
                    for target in predicates
                ],
            },
            item.alias,
        )
    else:
        builder.filter(
            EntityFilter(
                entity.id,
                "having",
                tuple(Condition(target.id, op, value) for target in predicates),
                over,
            )
        )
    return builder.build()


def _period_options(
    config: Any, builder_for: Any, entity: Any, predicates: list[Any], op: str, value: Any
) -> list[dict[str, Any]]:
    """One ready-to-send call per reading of an unstated qualification period."""
    base = builder_for().build()
    words = ["all_time"]
    if base.time is not None and base.time.window:
        words.append("window")
    if base.time is not None and base.time.grain:
        words.append("period")
    reads = {
        "all_time": f"every row of each {entity.label or entity.id}, whatever the window",
        "window": f"each {entity.label or entity.id}'s rows inside the query window",
        "period": f"each {entity.label or entity.id}'s rows in each period",
    }
    return [
        {
            "over": word,
            "reads": reads[word],
            "call": _qualified(config, builder_for, word, entity, predicates, op, value).to_call(),
        }
        for word in words
    ]


def _preferred_predicate_target(
    config: Any,
    metric_terms: list[str],
    *,
    target_measure: Any,
    entity: Any,
) -> Any | None:
    if not metric_terms:
        return None
    term_set = set(metric_terms)
    has_distinct = "distinct" in term_set
    # A measure each qualifying entity has one value of: its rows reach the entity.
    measures = [
        row for row in config.measures if entity.id in _reachable_entities(config, row.entity)
    ]
    # A measure whose whole name the qualification says ("orders" names Order count).
    content = {term for term in metric_terms if term not in _QUALIFICATION_STOP_TOKENS}
    _tied, named = _tied_top(measures, content, content)
    if named is not None:
        return named

    qualifier_prefixes = (
        "new_",
        "high_value_",
        "repeat_",
        "visiting_",
        "lifetime_",
        "delivered_",
        "drink_",
        "food_",
        "cumulative_",
    )
    measure_ranked: list[tuple[int, int, int, int, str, Any]] = []
    target_role = str(getattr(target_measure, "default_temporal_role", "") or "")
    for row in measures:
        score = _score(row, term_set)
        if score <= 0:
            continue
        role_match = 1 if str(getattr(row, "default_temporal_role", "") or "") == target_role else 0
        distinct_match = (
            1
            if (
                has_distinct
                and str(getattr(row, "measure_class", "") or "") == "distinct_population"
            )
            else 0
        )
        base_id = getattr(row, "id", "").split(".", 2)[-1] if getattr(row, "id", "") else ""
        unqualified_bonus = 1
        for prefix in qualifier_prefixes:
            if base_id.startswith(prefix):
                prefix_token = prefix.rstrip("_").split("_", 1)[0]
                if prefix_token not in term_set:
                    unqualified_bonus = 0
                    break
        measure_ranked.append(
            (-score, -unqualified_bonus, -role_match, -distinct_match, getattr(row, "id", ""), row)
        )
    measure_ranked.sort()

    metric_ranked: list[tuple[int, str, Any]] = []
    for row in config.metric_recipes:
        score = _score(row, term_set)
        if score <= 0:
            continue
        metric_ranked.append((-score, getattr(row, "id", ""), row))
    metric_ranked.sort()

    if measure_ranked:
        return measure_ranked[0][-1]
    if metric_ranked:
        return metric_ranked[0][-1]
    return _preferred_metric(config, term_set)


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    threshold = _threshold_from_text(text)
    if threshold is None:
        return None
    entity = _qualifying_entity(runtime._config, terms, text=text)
    if entity is None:
        return None

    target_terms = _target_measure_terms(text, terms)
    if not target_terms:
        return None
    target_measure = _preferred_measure(
        runtime._config, target_terms, _tokens(_target_focus_text(text))
    )
    if target_measure is None or not target_measure.default_temporal_role:
        return None

    predicate_targets: list[Any] = []
    predicate_term_groups = _predicate_metric_terms(text, terms, target_terms)
    for metric_terms in predicate_term_groups:
        target = _preferred_predicate_target(
            runtime._config, metric_terms, target_measure=target_measure, entity=entity
        )
        if target is not None and getattr(target, "id", "") not in {
            getattr(row, "id", "") for row in predicate_targets
        }:
            predicate_targets.append(target)

    op, value = threshold
    time_spec = _time_spec(target_measure.default_temporal_role, text)
    is_top, top_n = _top_n_intent(text)
    config = runtime._config
    group_by = _maybe_group_by(config, text, target_terms=target_terms)
    product_filter = _product_filter(config, "account") if "arr" in target_terms else None

    def builder_for() -> QueryBuilder:
        builder = QueryBuilder(config).measure(
            target_measure.id, _aggregation_from_text(text, terms, target_measure)
        )
        builder.time(time_spec).groups(group_by)
        if product_filter is not None:
            builder.filter(product_filter)
        if is_top:
            return builder.order(builder.build().select[0].alias, "desc").limit(top_n)
        return _add_order(builder)

    resolved = [_resolved(target_measure), *[_resolved(row) for row in predicate_targets]]
    resolved.append(_resolved(entity))
    if group_by and (dim := _object_by_id(config.dimensions, group_by[0])) is not None:
        resolved.append(_resolved(dim))
    intent = {
        "pattern": "qualified_metric_rollup",
        "source": "plan",
        "target_measure": target_measure.id,
        "predicate_metrics": [getattr(target, "id", "") for target in predicate_targets],
        "qualifying_entity": entity.id,
        "output_grain": time_spec.grain if time_spec is not None else "",
        "limit": top_n if is_top else None,
        "is_top_intent": is_top,
    }
    if not predicate_targets:
        # Pattern matched but no predicate resolved — surface as a
        # structured blocked draft so the orchestrator can return
        # recovery hints to the agent.
        phrase_groups = _qualification_phrase_token_groups(text, target_terms)
        return RuntimeCompositionDraft(
            query=builder_for().build(),
            resolved=[_resolved(target_measure), _resolved(entity)],
            rationale=[
                "matched qualified_metric_rollup pattern but could not resolve a predicate metric/measure for the qualification phrase",
            ],
            interpreted_intent=intent,
            blocked_reason={
                "code": "QUALIFICATION_NOT_REALIZABLE",
                "message": (
                    "Detected a qualified metric rollup but could not resolve a "
                    "predicate metric or measure from the qualification phrase."
                ),
                "details": {
                    "qualification_phrase": _qualification_phrase(text),
                    "qualification_tokens": [token for group in phrase_groups for token in group],
                    "target_measure": target_measure.id,
                    "qualifying_entity": entity.id,
                },
                "recovery_hints": [
                    "Rephrase the qualification so the predicate names a measure or metric — e.g. 'with at least 4 orders' or 'who placed more than 10 orders'.",
                    "Find available measures with discover, using the predicate keyword as terms.",
                ],
            },
        )
    over = _over(text, builder_for().build())
    query = _qualified(
        config, builder_for, over or "all_time", entity, predicate_targets, op, value
    )
    unstated = (
        {
            "code": "ENTITY_FILTER_PERIOD_UNSTATED",
            "message": (
                f"The question qualifies each {entity.label or entity.id} by its aggregate but "
                "doesn't say over which rows: every row it has, its rows in the query window, "
                "or its rows in each period."
            ),
            "details": {
                "clarification": {"question": "Over which rows is the qualification counted?"},
                "options": _period_options(
                    config, builder_for, entity, predicate_targets, op, value
                ),
            },
            "recovery_hints": [
                {"kind": "choose_over", "message": "Send the call of the reading you mean."}
            ],
        }
        if not over
        else {}
    )
    return RuntimeCompositionDraft(
        query=query,
        resolved=resolved,
        rationale=[
            "composed a qualified rollup: the target measure, filtered to the entities whose "
            "aggregate passes the qualification",
        ],
        interpreted_intent={**intent, "over": over},
        ask=unstated,
    )


PATTERN = IntentPattern(name="qualified_metric_rollup", match=_match)
