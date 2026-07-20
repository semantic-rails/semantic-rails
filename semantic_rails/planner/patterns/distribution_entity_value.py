"""Pattern: ``distribution_entity_value``.

Detects population-percentile intents over an entity-grain value
(e.g. "p80 ARR per account").
"""

from __future__ import annotations

from typing import Any

from .._base import (
    RuntimeCompositionDraft,
    _add_order,
    _entity,
    _measure,
    _object_text,
    _product_filter,
    _resolved,
    _time_spec,
)
from ._protocol import IntentPattern


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    config = runtime.config
    wants_distribution = "p80" in terms or "percentile" in terms or "80th" in terms
    if not wants_distribution or "arr" not in terms:
        return None
    account_entity = _entity(
        config,
        ["account"],
        exclude=[row.id for row in config.entities if "parent" in _object_text(row)],
    )
    if account_entity is None:
        return None

    arr = _measure(config, ["arr"])
    if arr is None or not arr.default_temporal_role:
        return None
    product_filter = _product_filter(config, "account")
    query: dict[str, Any] = {
        "version": 2,
        "select": [
            {
                "as": "p80_arr",
                "expression": {
                    "kind": "distribution",
                    "function": "percentile",
                    "p": 0.8,
                    "over": {
                        "kind": "entity_value",
                        "entity": account_entity.id,
                        "input": {"measure": arr.id, "aggregation": "sum"},
                        "where": [{"kind": "value_filter", "op": ">", "value": 0}],
                    },
                },
            }
        ],
        "time": _time_spec(arr.default_temporal_role, text),
    }
    if product_filter is not None:
        query["where"] = [product_filter]
    _add_order(query)
    return RuntimeCompositionDraft(
        query=query,
        resolved=[_resolved(arr), _resolved(account_entity)],
        rationale=[
            "composed distribution over entity-grain values from primitive measure metadata"
        ],
        interpreted_intent={"pattern": "distribution_entity_value", "source": "plan"},
    )


PATTERN = IntentPattern(name="distribution_entity_value", match=_match)
