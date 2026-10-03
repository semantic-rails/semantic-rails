"""A metric constraint governs the filters a caller writes inside an expression."""

from copy import deepcopy
from dataclasses import replace

import pytest

from semantic_rails import compiler
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

PACKAGE = "configs/semantic_rails/jaffle_shop"
INVENTORY = "measure.jaffle.inventory_on_hand_eop"
INVENTORY_DAY = "temporal_role.jaffle_inventory_day"
INVENTORY_STORE = "dimension.jaffle_store_inventory_snapshot_store_id"
STORE_NAME = "dimension.jaffle_store_name"


@pytest.fixture(scope="module")
def config():
    return replace(load_package_config(PACKAGE), semantic_policies=[])


def _constrained(config, constraint, governed, source_path=PACKAGE):
    policy = SemanticPolicyConfig(
        id="policy.test.inline_filters",
        kind="metric_constraint",
        object_ids=[governed] if governed else [],
        config=constraint,
    )
    return Runtime.from_config(
        replace(config, semantic_policies=[policy]), source_path=source_path
    )


def _assert_denied_before_output(engine, monkeypatch, query):
    def no_output(*args, **kwargs):
        pytest.fail("a constrained inline filter reached output")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(engine, "_compile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    result = engine.validate(query)
    assert result["errors"][0]["code"] == "POLICY_DENIED"
    for operation in (engine.compile, engine.query):
        with pytest.raises(SemanticLayerError) as exc:
            operation(query)
        assert exc.value.code == "POLICY_DENIED"
    return result["policy_effects"][0]["violations"]


def _closing_inventory(store_filter):
    return {
        "select": [
            {
                "expression": {
                    "kind": "semi_additive",
                    "measure": INVENTORY,
                    "filter": {"all": [store_filter]},
                },
                "as": "inventory",
            }
        ],
        "time": {"temporal_role": INVENTORY_DAY, "grain": "month"},
    }


def test_an_inline_filter_outside_allowed_where_is_refused(config, monkeypatch):
    """The closing-day value of a stock filtered inside its own expression."""
    query = _closing_inventory({"field": STORE_NAME, "op": "=", "value": "Brooklyn"})
    assert compiler.compile_query(config, None, query)["sql"]
    engine = _constrained(config, {"allowed_where": [INVENTORY_STORE]}, INVENTORY)
    try:
        violations = _assert_denied_before_output(engine, monkeypatch, query)
    finally:
        engine.close()
    assert violations == [
        {
            "kind": "disallowed_where",
            "disallowed": [STORE_NAME],
            "allowed": [INVENTORY_STORE],
            "source": "inline_expression",
        }
    ]
