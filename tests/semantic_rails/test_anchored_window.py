"""Per-row event-anchored windows on ``scoped_aggregate``: the call shape validates, and the
compiler refuses to run it until the SQL lowering exists (the ``feature_pending_sql_lowering``
recovery hint says so)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

FIRST_ORDER = {"temporal_role": "temporal_role.jaffle_customer_first_order_at"}
NINETY_DAYS = {"unit": "day", "value": 90, "direction": "forward"}
TOP_DECILE = {
    "kind": "metric_predicate",
    "entity": "entity.jaffle_customer",
    "input": {
        "kind": "measure",
        "measure": "measure.jaffle.lifetime_spend_usd",
        "aggregation": "sum",
    },
    "op": "gte",
    "value": {"kind": "percentile", "p": 0.9},
    "over": "all_time",
}


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """One runtime for the module: every call here only validates."""
    package_dir = copy_package_config(tmp_path_factory.mktemp("anchored"), "jaffle_shop")
    package = load_package_config(str(package_dir))
    rt = Runtime.from_config(package, source_path=str(package_dir), package_id="jaffle_shop")
    try:
        yield rt
    finally:
        rt.close()


@pytest.mark.parametrize(
    ("keys", "code", "details", "hint"),
    [
        # The canonical shape parses; the compile stub blocks it with a clear signal.
        pytest.param(
            {"anchor": FIRST_ORDER, "window": NINETY_DAYS},
            "INVALID_ANCHOR_ROLE",
            {"feature_status": "ir_contract_only"},
            "feature_pending_sql_lowering",
            id="canonical",
        ),
        pytest.param(
            {"window": {"unit": "day", "value": 90}},
            "INVALID_EXPRESSION_AST",
            {"missing_key": "anchor"},
            "fix_expression_shape",
            id="missing_anchor",
        ),
        pytest.param(
            {"anchor": FIRST_ORDER, "window": {"unit": "fortnight", "value": 6}},
            "INVALID_EXPRESSION_AST",
            {"received_unit": "fortnight"},
            "fix_expression_shape",
            id="invalid_unit",
        ),
        pytest.param(
            {"anchor": FIRST_ORDER, "window": {**NINETY_DAYS, "direction": "sideways"}},
            "INVALID_EXPRESSION_AST",
            {"received_direction": "sideways"},
            "fix_expression_shape",
            id="invalid_direction",
        ),
        # The available roles surface, in the details and the hint, so no inspect is needed.
        pytest.param(
            {"anchor": {"temporal_role": "temporal_role.missing"}, "window": NINETY_DAYS},
            "INVALID_ANCHOR_ROLE",
            {"anchor_role": "temporal_role.missing"},
            "use_valid_anchor_role",
            id="unknown_anchor_role",
        ),
        # A scoped aggregate without anchor and window keeps its percentile-threshold path.
        pytest.param({"predicates": [TOP_DECILE]}, None, {}, None, id="no_anchor_window"),
    ],
)
def test_anchored_window_calls(
    runtime: Runtime, keys: dict[str, Any], code: str | None, details: dict[str, Any], hint: str
) -> None:
    expression = {
        "kind": "scoped_aggregate",
        "measure": "measure.jaffle.revenue_usd",
        "aggregation": "sum",
        **keys,
    }
    result = runtime.validate({"select": [{"expression": expression, "as": "rev"}]})
    if code is None:
        assert result["ok"], result.get("errors")
        return
    error = result["errors"][0]
    assert error["code"] == code
    assert details.items() <= error["details"].items()
    (matched,) = [item for item in error["recovery_hints"] if item.get("kind") == hint]
    assert "docs/" not in matched["message"]
    if hint == "use_valid_anchor_role":
        assert FIRST_ORDER["temporal_role"] in error["details"]["available_temporal_roles"]
        assert FIRST_ORDER["temporal_role"] in matched["available_temporal_roles"]
