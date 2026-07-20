"""Pattern: ``scoped_predicate_ratio``.

Detects share/percentage intents that ratio a scoped aggregate filtered
by multi-channel metric predicates (e.g. "% of paying logos using both
SMS and push").
"""

from __future__ import annotations

from typing import Any

from .._base import (
    RuntimeCompositionDraft,
    _add_order,
    _entity,
    _maybe_group_by,
    _measure,
    _metric,
    _metric_predicate,
    _object_text,
    _product_filter,
    _resolved,
    _time_spec,
)
from ._protocol import IntentPattern


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    config = runtime.config
    wants_pct = "%" in str(text or "") or bool({"pct", "share", "ratio"} & terms)
    if not wants_pct or not {"sms", "push"} <= terms:
        return None

    account_entity = _entity(
        config,
        ["account"],
        exclude=[row.id for row in config.entities if "parent" in _object_text(row)],
    )
    sms_metric = _metric(config, ["sms"])
    push_metric = _metric(config, ["push"])
    if account_entity is None or sms_metric is None or push_metric is None:
        return None

    is_paying_logo = bool({"paying", "logo"} & terms)
    anchor_measure = (
        _measure(config, ["paying", "logo"]) if is_paying_logo else _measure(config, ["arr"])
    )
    if anchor_measure is None or not anchor_measure.default_temporal_role:
        return None
    product_value = "account" if is_paying_logo else ("email" if "email" in terms else "")
    product_filter = _product_filter(config, product_value) if product_value else None
    aggregation = (
        "count_distinct" if anchor_measure.measure_class == "distinct_population" else "sum"
    )
    scoped: dict[str, Any] = {
        "kind": "scoped_aggregate",
        "measure": anchor_measure.id,
        "aggregation": aggregation,
        **({"where": [product_filter]} if product_filter is not None else {}),
    }
    query: dict[str, Any] = {
        "version": 2,
        "select": [
            {
                "as": "pct_paying_logos_sent_push_and_sms"
                if is_paying_logo
                else "pct_email_arr_from_sms_push_senders",
                "expression": {
                    "kind": "ratio",
                    "numerator": {
                        **scoped,
                        "predicates": [
                            _metric_predicate(sms_metric, account_entity),
                            _metric_predicate(push_metric, account_entity),
                        ],
                    },
                    "denominator": scoped,
                },
            }
        ],
        "time": _time_spec(anchor_measure.default_temporal_role, text),
    }
    group_by = _maybe_group_by(config, text)
    if group_by:
        query["group_by"] = group_by
    _add_order(query)
    return RuntimeCompositionDraft(
        query=query,
        resolved=[
            _resolved(anchor_measure),
            _resolved(sms_metric),
            _resolved(push_metric),
            _resolved(account_entity),
        ],
        rationale=["composed scoped ratio with metric-derived entity predicates"],
        interpreted_intent={"pattern": "scoped_predicate_ratio", "source": "plan"},
    )


PATTERN = IntentPattern(name="scoped_predicate_ratio", match=_match)
