"""A nested compile (an entity filter's per-entity source) records what it reads as the outer
query does: policies see its objects, and the binding keeps its routes."""

from dataclasses import replace

import pytest

from semantic_rails import compiler
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

JAFFLE = "configs/semantic_rails/jaffle_shop"
NESTED_ONLY = "measure.jaffle.order_count"  # read only by the entity filter's source
REVENUE = "measure.jaffle.revenue_usd"
CALL = {
    "select": [REVENUE],
    "group_by": ["dimension.jaffle_store_name"],
    "where": [
        {
            "entity": "entity.jaffle_customer",
            "having": [{"field": NESTED_ONLY, "op": "gte", "value": 4}],
            "over": "all_time",
        }
    ],
}


@pytest.fixture(scope="module")
def config():
    return replace(load_package_config(JAFFLE), semantic_policies=[])


def test_a_nested_compile_records_its_objects_and_routes(config, monkeypatch):
    nested = []
    compile_nested = compiler._compile_query_sql_ast

    def counted(*args, **kwargs):
        nested.append(args)
        return compile_nested(*args, **kwargs)

    monkeypatch.setattr(compiler, "_compile_query_sql_ast", counted)
    bound = compiler.bind_query(config, None, read_call(CALL, config=config)[0])

    assert nested  # the entity filter's source compiled as its own query
    assert NESTED_ONLY in bound.object_ids
    assert not any(NESTED_ONLY in ids for ids in bound.leaf_objects.values())
    # The source groups orders by customer, a route the outer query never takes.
    assert ("entity.jaffle_order", "entity.jaffle_customer") in {
        (source, target) for source, target, _ in bound.route_choices
    }


def test_a_policy_on_a_nested_only_measure_refuses_the_call(config):
    deny = SemanticPolicyConfig(
        id="policy.test.nested", kind="object_access", object_ids=[NESTED_ONLY], action="deny"
    )
    for policies, denied in (([], False), ([deny], True)):
        runtime = Runtime.from_config(
            replace(config, semantic_policies=policies), source_path=JAFFLE
        )
        try:
            if denied:
                with pytest.raises(SemanticLayerError) as caught:
                    runtime.compile(CALL)
                assert caught.value.code == "POLICY_DENIED"
            else:
                assert runtime.compile(CALL)["rendered_sql"]
        finally:
            runtime.close()
