"""Directional safety for fact-table measures.

Ensures that a grouping dimension (a call's select names one only for the front door to
lift it into ``group_by``) is reachability-validated. A fact measure bound to a time entity
must not silently drop an unreachable customer-side dim from the output.
"""

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry


def test_fact_measure_with_unreachable_select_dim_errors():
    config = load_package_config("configs/semantic_rails/jaffle_shop")
    registry = Registry(config)
    customer_dim = next(d for d in config.dimensions if "customer_type" in d.id.lower())
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(
            config,
            registry,
            {
                "select": [
                    {
                        "expression": {"measure": "measure.jaffle.revenue_mtd_usd"},
                        "as": "mtd",
                    },
                ],
                "group_by": [customer_dim.id],
                "time": {
                    "temporal_role": "temporal_role.jaffle_daily_metric_day",
                    "grain": "day",
                },
            },
        )
    assert raised.value.code == "PATH_NOT_FOUND"
    assert raised.value.details["start"] == "entity.jaffle_time"
    assert raised.value.details["target"] == "entity.jaffle_customer"
