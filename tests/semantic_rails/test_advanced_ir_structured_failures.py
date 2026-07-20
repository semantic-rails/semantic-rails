"""Invalid advanced IR must fail structured, never internal or silent.

External-agent feedback round three: a probe battery of questionable
advanced expressions surfaced (a) an internal KeyError for unknown
``constant_properties`` dimensions, and (b) shapes that validated "ok"
while a semantic component was silently dropped (aggregate-level
``window`` specs) or silently misread (unknown ``dimension_bindings``
side values behaving as base-side; window units that only blow up at
warehouse execution). These tests pin the structured failure envelope.
"""

from __future__ import annotations


def _conversion_expr(**overrides) -> dict:
    expr = {
        "kind": "conversion",
        "entity": "entity.jaffle_customer",
        "window": {"unit": "day", "value": 28},
        "matching_mode": "first_converted_after_base",
        "base": {"kind": "aggregate", "measure": "measure.jaffle.order_count"},
        "converted": {"kind": "aggregate", "measure": "measure.jaffle.order_count"},
    }
    expr.update(overrides)
    return expr


def _validate_expr(runtime, expr: dict, **query_extra) -> dict:
    return runtime.validate(
        {"version": 2, "select": [{"as": "probe", "expression": expr}], **query_extra}
    )


def _codes(report: dict) -> list[str]:
    return [error.get("code") for error in report.get("errors", [])]


def test_unknown_constant_property_is_object_not_found_not_internal(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = _validate_expr(
        runtime, _conversion_expr(constant_properties=["dimension.does_not_exist"])
    )
    assert report["ok"] is False
    assert _codes(report) == ["OBJECT_NOT_FOUND"]


def test_unsupported_conversion_window_unit_fails_at_validate(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = _validate_expr(runtime, _conversion_expr(window={"unit": "fortnight", "value": 2}))
    assert report["ok"] is False
    assert _codes(report) == ["CONVERSION_WINDOW_REQUIRED"]
    details = report["errors"][0].get("details") or {}
    assert "supported_units" in details


def test_unknown_dimension_binding_side_is_rejected_not_silently_base(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = _validate_expr(
        runtime,
        _conversion_expr(dimension_bindings={"dimension.jaffle_store_name": {"side": "sideways"}}),
        group_by=["dimension.jaffle_store_name"],
    )
    assert report["ok"] is False
    assert _codes(report) == ["INVALID_EXPRESSION_AST"]


def test_unknown_dimension_binding_key_is_rejected(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = _validate_expr(
        runtime,
        _conversion_expr(
            dimension_bindings={"dimension.jaffle_store_name": {"denominator_policy": "x"}}
        ),
    )
    assert report["ok"] is False
    assert _codes(report) == ["INVALID_EXPRESSION_AST"]


def test_unsupported_denominator_policy_is_rejected(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = _validate_expr(
        runtime,
        _conversion_expr(
            dimension_bindings={
                "dimension.jaffle_store_name": {
                    "side": "converted",
                    "denominator": "matched_base_events",
                }
            }
        ),
    )
    assert report["ok"] is False
    assert _codes(report) == ["INVALID_EXPRESSION_AST"]


def test_aggregate_level_window_is_rejected_not_silently_dropped(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = _validate_expr(
        runtime,
        {
            "kind": "aggregate",
            "measure": "measure.jaffle.revenue_usd",
            "window": {"kind": "rolling", "unit": "day", "value": 7},
        },
    )
    assert report["ok"] is False
    assert _codes(report) == ["INVALID_EXPRESSION_AST"]
    assert "rolling" in report["errors"][0]["message"]


def test_supported_rolling_kind_still_validates(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = _validate_expr(
        runtime,
        {
            "kind": "rolling",
            "input": {"kind": "aggregate", "measure": "measure.jaffle.revenue_usd"},
            "window": {"unit": "day", "value": 7},
        },
        time={"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
    )
    assert report["ok"] is True, _codes(report)


def test_valid_dimension_binding_still_validates(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = _validate_expr(
        runtime,
        _conversion_expr(
            dimension_bindings={
                "dimension.jaffle_product_type": {
                    "side": "converted",
                    "denominator": "all_base_events",
                }
            }
        ),
        group_by=["dimension.jaffle_product_type"],
    )
    assert report["ok"] is True, _codes(report)
