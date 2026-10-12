"""Invalid advanced IR fails structured, never internal or silent.

Unknown ``constant_properties`` dimensions once raised an internal KeyError; an aggregate-level
``window`` was silently dropped; an unknown ``dimension_bindings`` side acted as base-side; and
an unsupported window unit failed only at warehouse execution. Each is now a structured refusal
at validate, while the supported shapes still validate.
"""

from __future__ import annotations

import pytest

from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

STORE = "dimension.jaffle_store_name"
REVENUE = {"kind": "aggregate", "measure": "measure.jaffle.revenue_usd"}


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    path = copy_package_config(tmp_path_factory.mktemp("package"), "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_snapshot(load_package_snapshot(path))
    yield runtime
    runtime.close()


def _conversion(**overrides) -> dict:
    return {
        "kind": "conversion",
        "entity": "entity.jaffle_customer",
        "window": {"unit": "day", "value": 28},
        "matching_mode": "first_converted_after_base",
        "base": {"kind": "aggregate", "measure": "measure.jaffle.order_count"},
        "converted": {"kind": "aggregate", "measure": "measure.jaffle.order_count"},
        **overrides,
    }


@pytest.mark.parametrize(
    ("expression", "extra", "code", "listed"),
    [
        pytest.param(
            _conversion(constant_properties=["dimension.does_not_exist"]),
            {},
            "OBJECT_NOT_FOUND",
            None,
            id="unknown-constant-property",
        ),
        pytest.param(
            _conversion(window={"unit": "fortnight", "value": 2}),
            {},
            "CONVERSION_WINDOW_REQUIRED",
            ("supported_units", "day"),
            id="unsupported-window-unit",
        ),
        pytest.param(
            _conversion(dimension_bindings={STORE: {"side": "sideways"}}),
            {"group_by": [STORE]},
            "INVALID_EXPRESSION_AST",
            None,
            id="unknown-binding-side",
        ),
        pytest.param(
            _conversion(dimension_bindings={STORE: {"denominator_policy": "x"}}),
            {},
            "INVALID_EXPRESSION_AST",
            None,
            id="unknown-binding-key",
        ),
        pytest.param(
            _conversion(
                dimension_bindings={
                    STORE: {"side": "converted", "denominator": "matched_base_events"}
                }
            ),
            {},
            "INVALID_EXPRESSION_AST",
            None,
            id="unsupported-denominator-policy",
        ),
        pytest.param(
            {**REVENUE, "window": {"kind": "rolling", "unit": "day", "value": 7}},
            {},
            "INVALID_EXPRESSION_AST",
            ("supported_window_kinds", "rolling"),
            id="aggregate-level-window",
        ),
        # The supported shapes the refusals point to still validate.
        pytest.param(
            {"kind": "rolling", "input": REVENUE, "window": {"unit": "day", "value": 7}},
            {"time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"}},
            None,
            None,
            id="rolling",
        ),
        pytest.param(
            _conversion(
                dimension_bindings={
                    "dimension.jaffle_product_type": {
                        "side": "converted",
                        "denominator": "all_base_events",
                    }
                }
            ),
            {"group_by": ["dimension.jaffle_product_type"]},
            None,
            None,
            id="dimension-binding",
        ),
    ],
)
def test_advanced_ir_validates_or_fails_structured(runtime, expression, extra, code, listed):
    report = runtime.validate(
        {"version": 1, "select": [{"as": "probe", "expression": expression}], **extra}
    )
    codes = [error.get("code") for error in report.get("errors", [])]
    if code is None:
        assert report["ok"] is True, codes
        return
    assert report["ok"] is False
    assert codes == [code]
    if listed is not None:
        key, member = listed
        assert member in report["errors"][0]["details"][key]
