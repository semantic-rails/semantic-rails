"""Column aggregates inherit constraints on measures reading the same source columns."""

from dataclasses import replace

import pytest

from semantic_rails import compiler
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

PACKAGE = "configs/semantic_rails/jaffle_shop"
REVENUE = "measure.jaffle.revenue_usd"
ORDER = "entity.jaffle_order"
STORE = "dimension.jaffle_store_name"
SUM_REVENUE = {
    "kind": "aggregate_if",
    "aggregation": "sum",
    "condition": {
        "kind": "comparison",
        "op": ">",
        "left": {"kind": "column", "entity": ORDER, "column": "order_total_cents"},
        "right": {"kind": "literal", "value": 0},
    },
    "value": {"kind": "column", "entity": ORDER, "column": "order_total_cents"},
}


@pytest.fixture(scope="module")
def config():
    return replace(load_package_config(PACKAGE), semantic_policies=[])


def _policy(constraint, governed=REVENUE, **scope):
    return SemanticPolicyConfig(
        id="policy.test.column_constraints",
        kind="metric_constraint",
        object_ids=[governed],
        config=constraint,
        **scope,
    )


def _query(expression=SUM_REVENUE, **extra):
    return {"select": [{"expression": expression, "as": "value"}], **extra}


@pytest.mark.parametrize(
    "constraint,kind",
    [
        ({"allowed_where": [STORE]}, "disallowed_where"),
        ({"allow_metric_filters": False}, "metric_filters_not_allowed"),
    ],
    ids=["allowed_where", "allow_metric_filters"],
)
def test_a_column_aggregate_cannot_bypass_a_measure_constraint(config, monkeypatch, constraint, kind):
    query = _query()
    assert compiler.compile_query(config, None, query)["sql"]
    engine = Runtime.from_config(
        replace(config, semantic_policies=[_policy(constraint)]), source_path=PACKAGE
    )

    def no_output(*args, **kwargs):
        pytest.fail("a forbidden column aggregate reached output")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(engine, "_compile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    try:
        result = engine.validate(query)
        assert result["errors"][0]["code"] == "POLICY_DENIED"
        assert kind in [row["kind"] for row in result["policy_effects"][0]["violations"]]
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as raised:
                operation(query)
            assert raised.value.code == "POLICY_DENIED"
    finally:
        engine.close()
