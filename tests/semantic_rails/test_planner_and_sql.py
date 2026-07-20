from __future__ import annotations

from dataclasses import replace

import pytest

import semantic_rails.compiler as compiler_module
from semantic_rails.compiler import compile_query
from semantic_rails.db import Database
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import (
    AggregateExpr,
    ArithmeticExpr,
    ColumnRefExpr,
    MetricRecipeRefExpr,
)
from semantic_rails.registry import Registry
from semantic_rails.schema import (
    DimensionConfig,
    EntityConfig,
    MeasureConfig,
    MetricConfig,
    PackageConfig,
    PackageMeta,
    RelationshipConfig,
)
from semantic_rails.sql_ast import SqlSelect


def _jaffle_query() -> dict:
    return {
        "select": [
            {
                "expression": {
                    "measure": "measure.jaffle.order_count",
                    "aggregation": "count_distinct",
                },
                "as": "orders",
            },
            {
                "expression": {"metric": "metric.sales.aov_usd"},
                "as": "aov_usd",
            },
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        "order_by": [{"field": "orders", "direction": "DESC"}],
        "limit": 5,
    }


def test_planner_explain_shape_and_sql_rendering(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = _jaffle_query()

    compiled = compile_query(config, Registry(config), query)
    plan = compiled["logical_plan"]
    sql_ast = compiled["sql_ast"]
    rendered = compiled["sql"]

    assert plan.version == 2
    assert plan.root_entity == "entity.jaffle_order"
    assert plan.selected_paths["entity.jaffle_store"] == ["relationship.orders_store"]
    assert plan.grain_analysis["root_entity"] == "entity.jaffle_order"
    assert plan.fanout_strategy["status"] == "ok"
    assert isinstance(sql_ast, SqlSelect)
    assert sql_ast.limit == 5
    # `projected` passthrough CTE is now inlined when there are no metric
    # filters; ORDER BY references the output alias directly.
    assert "COUNT(DISTINCT jaffle_order.order_id)" in rendered
    assert "orders DESC" in rendered
    assert compiled["explain"].physical_plan["nodes"]
    assert compiled["explain"].performance_plan["semantic_equivalence"] == "exact_primitives"
    assert compiled["explain"].performance_plan["risk_level"] == "low"
    assert compiled["explain"].performance_plan["date_filter_pushdown"][0]["pushed_to_scan"] is True
    assert "alias_resolution" in compiled["explain"].__dict__


def test_validate_rejects_unsupported_query_ir_version(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "version": 3,
                "select": [
                    {
                        "expression": {"measure": "measure.jaffle.order_count"},
                        "as": "orders",
                    }
                ],
            }
        )
    finally:
        runtime.close()
    assert report["ok"] is False
    assert report["errors"][0]["code"] == "INVALID_QUERY"
    assert report["errors"][0]["details"]["path"] == "version"


def test_invalid_temporal_role_is_reported_by_validate(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.order_count",
                            "aggregation": "count_distinct",
                        },
                        "as": "orders",
                    }
                ],
                "time": {"temporal_role": "t.does_not_exist", "grain": "month"},
            }
        )
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "INVALID_TEMPORAL_ROLE"
    finally:
        runtime.close()


def test_entity_in_terms_of_order_count_by_product_type_uses_item_anchor(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.order_count",
                            "aggregation": "count_distinct",
                        },
                        "as": "orders",
                    }
                ],
                "group_by": ["dimension.jaffle_product_type"],
            }
        )
        assert report["ok"] is True
        rendered = report["explain"]["rendered_sql"]
        assert "COUNT(DISTINCT jaffle_item.order_id)" in rendered
        assert "FROM jaffle_item" in rendered
        assert "jaffle_order" not in rendered
        rewrite_steps = report["logical_plan"]["rewrite_steps"]
        assert rewrite_steps[0]["kind"] == "entity_in_terms_of"
        scans = report["explain"]["performance_plan"]["estimated_scan_relations"]
        assert scans[0]["relation"] == "jaffle_item"
    finally:
        runtime.close()


def test_entity_in_terms_of_order_count_by_product_type_keeps_order_time_lookup(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.order_count",
                            "aggregation": "count_distinct",
                        },
                        "as": "orders",
                    }
                ],
                "group_by": ["dimension.jaffle_product_type"],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            }
        )
        assert report["ok"] is True
        rendered = report["explain"]["rendered_sql"]
        assert "FROM jaffle_item" in rendered
        assert "INNER JOIN jaffle_order ON jaffle_item.order_id = jaffle_order.order_id" in rendered
        assert "DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP))" in rendered

        join_nodes = [
            node for node in report["explain"]["physical_plan"]["nodes"] if node["kind"] == "Join"
        ]
        assert join_nodes
        assert join_nodes[0]["details"]["rewrite_reason"] == "entity_in_terms_of"
        assert join_nodes[0]["details"]["physical_anchor_entity"] == "entity.jaffle_item"
        assert join_nodes[0]["details"]["omitted_semantic_root_entity"] == "entity.jaffle_order"
        assert join_nodes[0]["details"]["linked_dimensions"] == [
            {
                "dimension_id": "dimension.jaffle_product_type",
                "target_entity": "entity.jaffle_product",
                "physical_anchor_entity": "entity.jaffle_item",
                "rewrite_reason": "entity_in_terms_of",
            }
        ]
    finally:
        runtime.close()


def test_entity_in_terms_of_order_count_by_product_type_allows_root_lookup_filter(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.order_count",
                            "aggregation": "count_distinct",
                        },
                        "as": "orders",
                    }
                ],
                "group_by": ["dimension.jaffle_product_type"],
                "where": [{"field": "dimension.jaffle_store_name", "op": "=", "value": "Brooklyn"}],
            }
        )
        assert report["ok"] is True
        rendered = report["explain"]["rendered_sql"]
        assert "FROM jaffle_item" in rendered
        assert "INNER JOIN jaffle_order ON jaffle_item.order_id = jaffle_order.order_id" in rendered
        assert (
            "INNER JOIN jaffle_store ON jaffle_order.store_id = jaffle_store.store_id" in rendered
        )
        assert "jaffle_store.store_name = 'Brooklyn'" in rendered
    finally:
        runtime.close()


def test_entity_in_terms_of_customer_count_by_product_type_omits_customer_table(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.customer_count",
                            "aggregation": "count_distinct",
                        },
                        "as": "customers",
                    }
                ],
                "group_by": ["dimension.jaffle_product_type"],
            }
        )
        assert report["ok"] is True
        rendered = report["explain"]["rendered_sql"]
        assert "COUNT(DISTINCT jaffle_order.customer_id)" in rendered
        assert "FROM jaffle_order" in rendered
        assert "JOIN jaffle_customer" not in rendered
        rewrite_steps = report["logical_plan"]["rewrite_steps"]
        assert rewrite_steps[0]["kind"] == "entity_in_terms_of"
        scans = report["explain"]["performance_plan"]["estimated_scan_relations"]
        assert scans[0]["relation"] == "jaffle_order"
    finally:
        runtime.close()


def test_entity_in_terms_of_customer_count_by_store_uses_order_anchor(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.customer_count",
                            "aggregation": "count_distinct",
                        },
                        "as": "customers",
                    }
                ],
                "group_by": ["dimension.jaffle_store_name"],
            }
        )
        assert report["ok"] is True
        rendered = report["explain"]["rendered_sql"]
        assert "COUNT(DISTINCT jaffle_order.customer_id)" in rendered
        assert "FROM jaffle_order" in rendered
        assert "JOIN jaffle_store" in rendered
        assert "jaffle_storefront_session" not in rendered
        assert "JOIN jaffle_customer" not in rendered
        rewrite_steps = report["logical_plan"]["rewrite_steps"]
        assert rewrite_steps[0]["kind"] == "entity_in_terms_of"
        scans = report["explain"]["performance_plan"]["estimated_scan_relations"]
        assert scans[0]["relation"] == "jaffle_order"
    finally:
        runtime.close()


def test_supply_component_cost_by_store_uses_consumed_item_cost_config(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        supply_report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.item_cost_usd",
                            "aggregation": "sum",
                        },
                        "as": "supply_cost",
                    }
                ],
                "group_by": ["dimension.jaffle_store_name"],
            }
        )
        assert supply_report["ok"] is True
        rendered = supply_report["explain"]["rendered_sql"]
        assert "jaffle_item.item_cost_cents / 100.0" in rendered
        assert "JOIN jaffle_order" in rendered
        assert "JOIN jaffle_store" in rendered
        assert "jaffle_supply" not in rendered
    finally:
        runtime.close()


def test_inventory_by_customer_type_remains_invalid(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        inventory_report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {"metric": "metric.sales.inventory_on_hand_eop"},
                        "as": "inventory",
                    }
                ],
                "group_by": ["dimension.jaffle_customer_type"],
            }
        )
        assert inventory_report["ok"] is False
        assert inventory_report["errors"][0]["code"] in {
            "AMBIGUOUS_PATH",
            "PATH_NOT_FOUND",
            "MIXED_GRAIN_INVALID",
        }
    finally:
        runtime.close()


def test_product_grain_rolling_revenue_uses_item_revenue(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {"metric": "metric.sales.rolling_7d_revenue_direct"},
                        "as": "rev_7d",
                    }
                ],
                "group_by": ["dimension.jaffle_product_type"],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
            }
        )
        assert report["ok"] is True
        rendered = report["explain"]["rendered_sql"]
        assert "jaffle_item.item_revenue_cents / 100.0" in rendered
        assert "JOIN jaffle_product" in rendered
    finally:
        runtime.close()


def test_relative_time_range_expansion(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue"}
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "month",
                    "range": {"last": {"unit": "month", "value": 3}},
                },
                "policy_context": {"now": "2026-05-04"},
            }
        )
        assert report["ok"] is True
        assert report["normalized_query"]["time"]["start"] == "2026-02-01"
        assert report["normalized_query"]["time"]["end"] == "2026-05-01"
        rendered = report["explain"]["rendered_sql"]
        assert "2026-02-01" in rendered
        assert "2026-05-01" in rendered
    finally:
        runtime.close()


def test_time_range_last_string_shorthand_rejected_with_recovery_hint(runtime_factory):
    """The schema description used to say `{last: '90 days'}` (string),
    but the validator only accepts `{last: {unit, value}}`. Now the
    schema matches the validator AND the INVALID_QUERY error carries a
    `USE_OBJECT_SHAPE` recovery hint pointing at the corrected shape."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue"}
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "month",
                    "range": {"last": "90 days"},
                },
            }
        )
        assert report["ok"] is False, "expected validation failure on string shorthand"
        errors = list(report.get("errors", []) or [])
        invalid_query = next(
            (e for e in errors if str(e.get("code", "")) == "INVALID_QUERY"),
            None,
        )
        assert invalid_query is not None, f"expected INVALID_QUERY in errors; got {errors}"
        details = dict(invalid_query.get("details", {}) or {})
        assert details.get("path") == "time.range.last"
        # Recovery hint must be present (either at the top of the report
        # or under details.recovery_hints, depending on the response
        # shape callers see) and name the corrected shape.
        all_hints = list(
            report.get("recovery_hints", []) or details.get("recovery_hints", []) or []
        )
        assert all_hints, (
            f"expected recovery_hints on time.range.last error; got error={invalid_query}, "
            f"top-level={report.get('recovery_hints', [])}"
        )
        joined = " ".join(str(h.get("message", "")) for h in all_hints)
        assert "unit" in joined and "value" in joined, (
            f"recovery hint should name unit and value; got {joined!r}"
        )
    finally:
        runtime.close()


def test_where_payload_validation(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue"}
                ],
                "where": [
                    {"expression": {"kind": "literal", "value": True}, "op": "=", "value": True}
                ],
            }
        )
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "INVALID_EXPRESSION_AST"
        assert report["errors"][0]["details"]["path"] == "where[0].field"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("object_kind", "query", "expected_closest_assertion"),
    [
        # Typo a metric ref → validate() returns OBJECT_NOT_FOUND.
        # ``metric.sales.revenue_usd`` was a dropped wrapper, so the
        # closest-matches point at surviving revenue-themed metrics
        # (YTD/QTD family or other revenue-related sales metrics).
        (
            "metric",
            {
                "version": 1,
                "select": [{"expression": {"metric": "metric.sales.reveneu_usd"}, "as": "revenue"}],
            },
            lambda closest: any(c.startswith("metric.sales.revenue_") for c in closest[:3]),
        ),
        # Typo a dimension id via group_by → validate() returns
        # OBJECT_NOT_FOUND, canonical id surfaces in closest_matches.
        (
            "dimension",
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                        },
                        "as": "rev",
                    }
                ],
                "group_by": ["dimension.jaffle_store_nam"],
            },
            lambda closest: "dimension.jaffle_store_name" in closest,
        ),
        # Typo a measure id → validate() returns OBJECT_NOT_FOUND.
        (
            "measure",
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.revneue_usd",
                            "aggregation": "sum",
                        },
                        "as": "rev",
                    }
                ],
            },
            lambda closest: "measure.jaffle.revenue_usd" in closest,
        ),
    ],
    ids=["metric_ref", "dimension_group_by", "measure_ref"],
)
def test_validate_object_not_found_surfaces_closest_matches(
    runtime_factory, object_kind, query, expected_closest_assertion
):
    """Validate-path OBJECT_NOT_FOUND must enrich every object kind with
    a ``closest_matches`` block — this is the typo-recovery contract for
    metric, dimension, and measure refs."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(query)
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "OBJECT_NOT_FOUND"
        closest = report["errors"][0]["details"]["closest_matches"]
        assert expected_closest_assertion(closest), (
            f"{object_kind} closest_matches did not satisfy assertion: {closest}"
        )
    finally:
        runtime.close()


def test_object_not_found_close_matches_on_compile(runtime_factory):
    """The compile path raises rather than returning a validation envelope.
    The raised SemanticLayerError must still carry closest_matches.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        with pytest.raises(SemanticLayerError) as exc_info:
            runtime.compile(
                {
                    "version": 1,
                    "select": [
                        {
                            "expression": {
                                "measure": "measure.jaffle.revenue_usd",
                                "aggregation": "sum",
                            },
                            "as": "rev",
                        }
                    ],
                    "group_by": ["dimension.jaffle_store_nam"],
                }
            )
        assert exc_info.value.code == "OBJECT_NOT_FOUND"
        closest = list(exc_info.value.details.get("closest_matches", []))
        assert "dimension.jaffle_store_name" in closest
    finally:
        runtime.close()


def test_invalid_temporal_role_close_matches(runtime_factory):
    """An unknown temporal_role surfaces near-matches even though it
    raises INVALID_TEMPORAL_ROLE rather than OBJECT_NOT_FOUND."""
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                        },
                        "as": "rev",
                    }
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_tim", "grain": "month"},
            }
        )
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "INVALID_TEMPORAL_ROLE"
        closest = report["errors"][0]["details"]["closest_matches"]
        assert "temporal_role.jaffle_order_time" in closest
    finally:
        runtime.close()


def test_object_not_found_close_matches_on_segment(runtime_factory):
    """segment_validate enriches OBJECT_NOT_FOUND with closest_matches."""
    runtime = runtime_factory("jaffle_shop")
    try:
        # Pick any valid segment to learn the namespace, then typo it.
        valid_segments = [seg.id for seg in runtime.config.segments]
        if not valid_segments:
            pytest.skip("jaffle_shop ships no segments to typo against")
        canonical = valid_segments[0]
        # Typo the canonical id by chopping the last character.
        typo = canonical[:-1]
        report = runtime.segment_validate(typo)
        assert report["ok"] is False
        err = report["errors"][0]
        assert err["code"] == "OBJECT_NOT_FOUND"
        closest = list(err["details"].get("closest_matches", []))
        assert closest, f"expected closest_matches for {typo!r}, got {err}"
    finally:
        runtime.close()


def test_duplicate_output_aliases_are_rejected(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        duplicate_select = runtime.validate(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "dup"},
                    {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "dup"},
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            }
        )
        duplicate_group = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {"measure": "measure.jaffle.order_count"},
                        "as": "dimension.jaffle_store_name",
                    },
                ],
                "group_by": ["dimension.jaffle_store_name"],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            }
        )
        duplicate_time = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {"measure": "measure.jaffle.order_count"},
                        "as": "temporal_role.jaffle_order_time__month",
                    },
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            }
        )

        assert duplicate_select["ok"] is False
        assert duplicate_select["errors"][0]["code"] == "DUPLICATE_OUTPUT_ALIAS"
        assert duplicate_group["ok"] is False
        assert duplicate_group["errors"][0]["code"] == "DUPLICATE_OUTPUT_ALIAS"
        assert duplicate_time["ok"] is False
        assert duplicate_time["errors"][0]["code"] == "DUPLICATE_OUTPUT_ALIAS"
    finally:
        runtime.close()


def test_group_by_only_distinct_values_compile_without_measures(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    compiled = compile_query(
        config,
        Registry(config),
        {
            "version": 1,
            "group_by": ["dimension.jaffle_store_name"],
            "order_by": [{"field": "dimension.jaffle_store_name", "direction": "ASC"}],
            "limit": 5,
        },
    )

    assert compiled["logical_plan"].root_entity == "entity.jaffle_store"
    assert compiled["logical_plan"].bound_measures == []
    assert compiled["logical_plan"].measure_plans == []
    assert "FROM jaffle_store" in compiled["sql"]
    # `projected` passthrough CTE inlined; ORDER BY uses output alias.
    assert "g1 ASC" in compiled["sql"]


def test_time_only_distinct_values_compile_with_explicit_temporal_role(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    compiled = compile_query(
        config,
        Registry(config),
        {
            "version": 1,
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "order_by": [{"field": "time", "direction": "ASC"}],
            "limit": 3,
        },
    )

    assert compiled["logical_plan"].root_entity == "entity.jaffle_order"
    assert compiled["logical_plan"].bound_measures == []
    assert "DATE_TRUNC('month'" in compiled["sql"]
    # `projected` passthrough CTE inlined; ORDER BY uses output alias.
    assert "ORDER BY\n  t ASC" in compiled["sql"]


def test_time_only_distinct_values_require_explicit_temporal_role(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate({"version": 1, "time": {"grain": "month"}})
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "INVALID_QUERY"
    finally:
        runtime.close()


def test_incompatible_temporal_role_is_reported_for_measure_and_metric(runtime_factory):
    jaffle = runtime_factory("jaffle_shop")
    try:
        measure_report = jaffle.validate(
            {
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.item_revenue_usd",
                            "aggregation": "sum",
                        },
                        "as": "item_revenue_usd",
                    }
                ],
                "time": {"temporal_role": "temporal_role.jaffle_store_opened_at", "grain": "month"},
            }
        )
        metric_report = jaffle.validate(
            {
                "select": [
                    {
                        "expression": {"metric": "metric.sales.rolling_28d_revenue"},
                        "as": "rolling_28d_revenue",
                    }
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            }
        )
        jaffle_metric_report = jaffle.validate(
            {
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
                "time": {"temporal_role": "temporal_role.jaffle_store_opened_at", "grain": "month"},
            }
        )

        assert measure_report["ok"] is False
        assert measure_report["errors"][0]["code"] == "INCOMPATIBLE_TEMPORAL_ROLE"
        assert metric_report["ok"] is False
        assert metric_report["errors"][0]["code"] == "INCOMPATIBLE_TEMPORAL_ROLE"
        assert jaffle_metric_report["ok"] is False
        assert jaffle_metric_report["errors"][0]["code"] == "INCOMPATIBLE_TEMPORAL_ROLE"
    finally:
        jaffle.close()


def test_mixed_grain_measures_are_rewritten_into_leaf_aggregates(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = {
        "select": [
            {
                "expression": {
                    "measure": "measure.jaffle.order_count",
                    "aggregation": "count_distinct",
                },
                "as": "orders",
            },
            {
                "expression": {
                    "measure": "measure.jaffle.item_count",
                    "aggregation": "count_distinct",
                },
                "as": "items",
            },
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    }

    compiled = compile_query(config, Registry(config), query)

    assert compiled["logical_plan"].fanout_strategy["status"] == "rewritten"
    assert any(
        step.measure_id == "measure.jaffle.item_count"
        for step in compiled["logical_plan"].rewrite_steps
    )
    assert "FULL OUTER JOIN leaf_2 AS right_side" in compiled["sql"]


def test_leaf_aggregate_rewrite_warning_is_classified_as_safe_alignment(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.order_count",
                            "aggregation": "count_distinct",
                        },
                        "as": "orders",
                    },
                    {
                        "expression": {
                            "measure": "measure.jaffle.item_count",
                            "aggregation": "count_distinct",
                        },
                        "as": "items",
                    },
                ],
                "group_by": ["dimension.jaffle_store_name"],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            }
        )
        warnings = list(report.get("warnings") or [])
        rewrite = next(w for w in warnings if w["code"] == "REWRITE_APPLIED")
        assert rewrite["message"].startswith("Safe cross-fact alignment")
        assert rewrite["details"]["rewrite_kind"] == "leaf_preaggregate_join"
        assert rewrite["details"]["rewrite_classification"] == "safe_independent_leaf_alignment"
    finally:
        runtime.close()


def test_same_fact_measures_fold_into_one_leaf_scan(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = {
        "select": [
            {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue"},
            {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        "limit": 10,
    }

    compiled = compile_query(config, Registry(config), query)
    rendered = compiled["sql"]

    assert "FULL OUTER JOIN" not in rendered
    assert "leaf_2 AS (" not in rendered
    assert rendered.count("FROM jaffle_order") == 1
    assert "leaf_base AS (" not in rendered
    assert "projected AS (" not in rendered
    assert compiled["explain"].performance_plan["raw_fact_count"] == 1
    assert len(compiled["explain"].performance_plan["estimated_scan_relations"][0]["measures"]) == 2
    assert compiled["explain"].performance_plan["full_outer_alignments"] == 0
    assert any(
        row["kind"] == "same_source_measure_folding"
        for row in compiled["physical_plan"].optimizations
    )


def test_alias_tiering_uses_short_internal_columns_and_preserves_final_names(
    package_config_factory,
):
    config, _ = package_config_factory("jaffle_shop")
    query = {
        "select": [
            {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue"},
            {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    }

    rendered = compile_query(config, Registry(config), query)["sql"]

    leaf_sql, final_sql = rendered.split(")\nSELECT", 1)
    assert " AS g1" in leaf_sql
    assert " AS t" in leaf_sql
    assert " AS m1" in leaf_sql
    assert " AS m2" in leaf_sql
    assert '"dimension.jaffle_store_name"' not in leaf_sql
    assert '"temporal_role.jaffle_order_time__month"' not in leaf_sql
    assert 'base.g1 AS "dimension.jaffle_store_name"' in final_sql
    assert 'base.t AS "temporal_role.jaffle_order_time__month"' in final_sql
    assert "base.m1 AS revenue" in final_sql
    assert "base.m2 AS orders" in final_sql


def test_bounded_time_filters_are_pushed_into_source_scan(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue"}],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "month",
            "start": "2017-01-01",
            "end": "2017-04-01",
        },
    }

    compiled = compile_query(config, Registry(config), query)
    rendered = compiled["sql"]
    performance_plan = compiled["explain"].performance_plan
    source_start = rendered.index("FROM jaffle_order")
    source_group = rendered.index("GROUP BY", source_start)
    source_scan = rendered[source_start:source_group]

    assert performance_plan["risk_level"] == "low"
    assert performance_plan["date_filter_pushdown"][0]["has_start"] is True
    assert performance_plan["date_filter_pushdown"][0]["has_end"] is True
    assert "jaffle_order.ordered_at >= '2017-01-01'" in source_scan
    assert "jaffle_order.ordered_at < '2017-04-01'" in source_scan


def test_multi_fact_metrics_align_on_common_metric_time(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = {
        "select": [
            {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
            {
                "expression": {"measure": "measure.jaffle.delivered_revenue_usd"},
                "as": "delivered_revenue",
            },
        ],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    }

    compiled = compile_query(config, Registry(config), query)
    rendered = compiled["sql"]

    assert compiled["logical_plan"].fanout_strategy["status"] == "rewritten"
    assert "FULL OUTER JOIN leaf_2 AS right_side" in rendered
    assert "DATE_TRUNC('month', CAST(jaffle_order.ordered_at AS TIMESTAMP)) AS t" in rendered
    assert (
        "DATE_TRUNC('month', CAST(jaffle_order_lifecycle.delivered_at AS TIMESTAMP)) AS t"
        in rendered
    )


def _primitive_channel_config() -> PackageConfig:
    entities = [
        EntityConfig(
            id="entity.email_fact", table="email_fact", primary_key="email_id", key=["email_id"]
        ),
        EntityConfig(id="entity.sms_fact", table="sms_fact", primary_key="sms_id", key=["sms_id"]),
        EntityConfig(
            id="entity.push_fact", table="push_fact", primary_key="push_id", key=["push_id"]
        ),
        EntityConfig(
            id="entity.account", table="account_dim", primary_key="account_id", key=["account_id"]
        ),
        EntityConfig(
            id="entity.parent_account",
            table="parent_account_dim",
            primary_key="account_id",
            key=["account_id"],
        ),
    ]
    dimensions = [
        DimensionConfig(
            id="dimension.parent_account_geo",
            entity="entity.parent_account",
            column="account_geo",
            data_type="string",
        ),
    ]
    relationships = [
        RelationshipConfig(
            id="relationship.email_account",
            source_entity="entity.email_fact",
            target_entity="entity.account",
            source_column="account_id",
            target_column="account_id",
            cardinality="N:1",
            safety="safe",
        ),
        RelationshipConfig(
            id="relationship.sms_account",
            source_entity="entity.sms_fact",
            target_entity="entity.account",
            source_column="account_id",
            target_column="account_id",
            cardinality="N:1",
            safety="safe",
        ),
        RelationshipConfig(
            id="relationship.push_account",
            source_entity="entity.push_fact",
            target_entity="entity.account",
            source_column="account_id",
            target_column="account_id",
            cardinality="N:1",
            safety="safe",
        ),
        RelationshipConfig(
            id="relationship.account_parent",
            source_entity="entity.account",
            target_entity="entity.parent_account",
            source_column="parent_account_id",
            target_column="account_id",
            cardinality="N:1",
            safety="safe",
        ),
    ]
    measures = [
        MeasureConfig(
            id="measure.email_count",
            entity="entity.email_fact",
            subject_entity="entity.email_fact",
            aggregation_entity="entity.email_fact",
            row_grain=["email_id"],
            expr=ColumnRefExpr("email_count"),
            default_aggregation="sum",
            allowed_aggregations=["sum"],
        ),
        MeasureConfig(
            id="measure.sms_count",
            entity="entity.sms_fact",
            subject_entity="entity.sms_fact",
            aggregation_entity="entity.sms_fact",
            row_grain=["sms_id"],
            expr=ColumnRefExpr("sms_count"),
            default_aggregation="sum",
            allowed_aggregations=["sum"],
        ),
        MeasureConfig(
            id="measure.push_count",
            entity="entity.push_fact",
            subject_entity="entity.push_fact",
            aggregation_entity="entity.push_fact",
            row_grain=["push_id"],
            expr=ColumnRefExpr("push_count"),
            default_aggregation="sum",
            allowed_aggregations=["sum"],
        ),
    ]
    recipes = [
        MetricConfig(
            id="metric.email_count",
            kind="aggregate",
            expression=AggregateExpr("measure.email_count", "sum"),
        ),
        MetricConfig(
            id="metric.sms_count",
            kind="aggregate",
            expression=AggregateExpr("measure.sms_count", "sum"),
        ),
        MetricConfig(
            id="metric.push_count",
            kind="aggregate",
            expression=AggregateExpr("measure.push_count", "sum"),
        ),
        MetricConfig(
            id="metric.total_messages",
            kind="derived",
            expression=ArithmeticExpr(
                "add",
                ArithmeticExpr(
                    "add",
                    MetricRecipeRefExpr("metric.email_count"),
                    MetricRecipeRefExpr("metric.sms_count"),
                    null_behavior="coalesce_zero",
                ),
                MetricRecipeRefExpr("metric.push_count"),
                null_behavior="coalesce_zero",
            ),
        ),
    ]
    return PackageConfig(
        version=3,
        package=PackageMeta(
            package_id="primitive_channels",
            name="Primitive channels",
            description="",
            warehouse="duckdb",
        ),
        entities=entities,
        dimensions=dimensions,
        temporal_roles=[],
        relationships=relationships,
        value_domains=[],
        measures=measures,
        metric_recipes=recipes,
    )


def test_primitive_only_parent_geo_plan_preaggregates_channel_facts_before_dimension_joins():
    config = _primitive_channel_config()
    query = {
        "version": 1,
        "select": [
            {"expression": {"metric": "metric.total_messages"}, "as": "total_messages"},
        ],
        "group_by": ["dimension.parent_account_geo"],
    }

    compiled = compile_query(config, Registry(config), query)
    rendered = compiled["sql"]
    performance_plan = compiled["explain"].performance_plan

    assert performance_plan["semantic_equivalence"] == "exact_primitives"
    assert performance_plan["raw_fact_count"] == 3
    assert performance_plan["full_outer_alignments"] == 2
    assert performance_plan["risk_level"] == "high"
    assert performance_plan["execution_recommendation"] == "needs_acceleration_layer"
    assert "primitive_exact_but_expensive" in performance_plan["notes"]
    assert performance_plan["joins_before_aggregate"] == []
    assert len(performance_plan["joins_after_aggregate"]) == 3
    assert all(
        row["strategy"] == "source_preaggregate"
        for row in performance_plan["joins_after_aggregate"]
    )
    assert "leaf_1_source_rollup AS (" in rendered
    assert "leaf_2_source_rollup AS (" in rendered
    assert "leaf_3_source_rollup AS (" in rendered
    for relation in ("email_fact", "sms_fact", "push_fact"):
        source_start = rendered.index(f"FROM {relation}")
        source_group = rendered.index("GROUP BY", source_start)
        assert "JOIN" not in rendered[source_start:source_group]


def test_parent_rollup_rejects_non_additive_aggregation_without_sketch_metadata():
    config = _primitive_channel_config()
    unsafe_measure = MeasureConfig(
        id="measure.email_average_per_parent",
        entity="entity.email_fact",
        subject_entity="entity.email_fact",
        aggregation_entity="entity.parent_account",
        row_grain=["email_id"],
        expr=ColumnRefExpr("email_count"),
        default_aggregation="avg",
        allowed_aggregations=["avg"],
    )
    config = replace(
        config,
        measures=[*config.measures, unsafe_measure],
        metric_recipes=[
            *config.metric_recipes,
            MetricConfig(
                id="metric.email_average_per_parent",
                kind="aggregate",
                expression=AggregateExpr("measure.email_average_per_parent", "avg"),
            ),
        ],
    )

    with pytest.raises(SemanticLayerError) as exc:
        compile_query(
            config,
            Registry(config),
            {
                "version": 1,
                "select": [
                    {
                        "expression": {"metric": "metric.email_average_per_parent"},
                        "as": "email_average",
                    }
                ],
                "group_by": ["dimension.parent_account_geo"],
            },
        )

    assert exc.value.code == "ROLLUP_UNSAFE"
    assert exc.value.details["unsupported_construct"] == "non_additive_parent_rollup"


def test_duckdb_synthetic_parent_geo_total_messages_matches_oracle():
    config = _primitive_channel_config()
    query = {
        "version": 1,
        "select": [{"expression": {"metric": "metric.total_messages"}, "as": "total_messages"}],
        "group_by": ["dimension.parent_account_geo"],
        "order_by": [{"field": "dimension.parent_account_geo", "direction": "ASC"}],
    }
    compiled = compile_query(config, Registry(config), query)
    db = Database.connect_in_memory()
    try:
        db.execute_script(
            """
            CREATE TABLE email_fact(email_id INTEGER, account_id TEXT, email_count INTEGER);
            CREATE TABLE sms_fact(sms_id INTEGER, account_id TEXT, sms_count INTEGER);
            CREATE TABLE push_fact(push_id INTEGER, account_id TEXT, push_count INTEGER);
            CREATE TABLE account_dim(account_id TEXT, parent_account_id TEXT);
            CREATE TABLE parent_account_dim(account_id TEXT, account_geo TEXT);

            INSERT INTO email_fact VALUES (1, 'a1', 10), (2, 'a1', 5), (3, 'a2', 7), (4, 'a3', 2);
            INSERT INTO sms_fact VALUES (1, 'a1', 3), (2, 'a2', 11), (3, 'a4', 100);
            INSERT INTO push_fact VALUES (1, 'a2', 4), (2, 'a3', 8);
            INSERT INTO account_dim VALUES ('a1', 'p1'), ('a2', 'p1'), ('a3', 'p2'), ('a4', 'p3');
            INSERT INTO parent_account_dim VALUES ('p1', 'US'), ('p2', 'EU'), ('p3', 'US');
            """
        )
        actual = db.query(compiled["sql"])
        oracle = db.query(
            """
            WITH channel_rows AS (
              SELECT p.account_geo, SUM(e.email_count) AS messages
              FROM email_fact e
              JOIN account_dim a ON e.account_id = a.account_id
              JOIN parent_account_dim p ON a.parent_account_id = p.account_id
              GROUP BY p.account_geo
              UNION ALL
              SELECT p.account_geo, SUM(s.sms_count) AS messages
              FROM sms_fact s
              JOIN account_dim a ON s.account_id = a.account_id
              JOIN parent_account_dim p ON a.parent_account_id = p.account_id
              GROUP BY p.account_geo
              UNION ALL
              SELECT p.account_geo, SUM(u.push_count) AS messages
              FROM push_fact u
              JOIN account_dim a ON u.account_id = a.account_id
              JOIN parent_account_dim p ON a.parent_account_id = p.account_id
              GROUP BY p.account_geo
            )
            SELECT account_geo AS "dimension.parent_account_geo", SUM(messages) AS total_messages
            FROM channel_rows
            GROUP BY account_geo
            ORDER BY account_geo
            """
        )
    finally:
        db.close()

    assert actual == oracle


def test_runtime_binary_expression_can_compose_mixed_clock_metric_primitives(
    package_config_factory,
):
    config, _ = package_config_factory("jaffle_shop")
    query = {
        "select": [
            {
                "expression": {
                    "kind": "binary",
                    "op": "plus",
                    "left": {"kind": "aggregate", "measure": "measure.jaffle.order_count"},
                    "right": {
                        "kind": "aggregate",
                        "measure": "measure.jaffle.delivered_revenue_usd",
                    },
                },
                "as": "orders_plus_delivered_revenue",
            }
        ],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    }

    rendered = compile_query(config, Registry(config), query)["sql"]

    assert "FULL OUTER JOIN leaf_2 AS right_side" in rendered
    # Top-level binary in SELECT list renders without redundant outer parens.
    assert "base.m1 + base.m2" in rendered


def test_scoped_ratio_uses_cross_domain_metric_predicate_sets(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = {
        "version": 2,
        "select": [
            {
                "as": "inventory_from_ordering_stores_share",
                "expression": {
                    "kind": "ratio",
                    "numerator": {
                        "kind": "scoped_aggregate",
                        "measure": "measure.jaffle.inventory_on_hand_eop",
                        "aggregation": "sum",
                        "predicates": [
                            {
                                "measure": "measure.jaffle.order_count",
                                "entity": "entity.jaffle_store",
                                "op": ">",
                                "value": 0,
                            }
                        ],
                    },
                    "denominator": {
                        "kind": "scoped_aggregate",
                        "measure": "measure.jaffle.inventory_on_hand_eop",
                        "aggregation": "sum",
                    },
                },
            }
        ],
        "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
    }

    compiled = compile_query(config, Registry(config), query)
    rendered = compiled["sql"]

    assert "predicate_jaffle_store_set_1 AS (" in rendered
    assert 'AS "temporal_role.jaffle_inventory_day__month"' in rendered
    assert "LEFT JOIN predicate_jaffle_store_set_1" in rendered
    assert "FULL OUTER JOIN" not in rendered
    assert rendered.count("FROM jaffle_store_inventory_snapshot") == 1
    assert any(
        row["kind"] == "anchored_entity_set" for row in compiled["physical_plan"].optimizations
    )
    assert any(row.kind == "PredicateSet" for row in compiled["logical_plan"].semantic_dag.nodes)
    assert any(row.kind == "ScopedSemiJoin" for row in compiled["logical_plan"].semantic_dag.nodes)
    assert any(
        row.kind == "PostAggregateCalc" and row.details.get("op") == "ratio"
        for row in compiled["logical_plan"].semantic_dag.nodes
    )


def test_anchored_ratio_delays_dimension_joins_and_prunes_predicate_context(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = {
        "version": 2,
        "select": [
            {
                "as": "inventory_from_ordering_stores_share",
                "expression": {
                    "kind": "ratio",
                    "numerator": {
                        "kind": "scoped_aggregate",
                        "measure": "measure.jaffle.inventory_on_hand_eop",
                        "aggregation": "sum",
                        "predicates": [
                            {
                                "measure": "measure.jaffle.order_count",
                                "entity": "entity.jaffle_store",
                                "op": ">",
                                "value": 0,
                            }
                        ],
                    },
                    "denominator": {
                        "kind": "scoped_aggregate",
                        "measure": "measure.jaffle.inventory_on_hand_eop",
                        "aggregation": "sum",
                    },
                },
            }
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {
            "temporal_role": "temporal_role.jaffle_inventory_day",
            "grain": "month",
            "start": "2024-01-01",
            "end": "2024-02-01",
        },
    }

    compiled = compile_query(config, Registry(config), query)
    rendered = compiled["sql"]

    predicate_section = rendered.split("predicate_jaffle_store_set_1 AS (", 1)[0]
    assert "jaffle_store.store_name" not in predicate_section
    assert "INNER JOIN jaffle_store ON" in rendered
    assert "LEFT JOIN predicate_jaffle_store_set_1" in rendered
    assert "FULL OUTER JOIN" not in rendered
    assert compiled["explain"].performance_plan["full_outer_alignments"] == 0


def test_anchored_ratio_uses_snowflake_qualify_for_snapshot_selection(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    config = replace(config, package=replace(config.package, warehouse="snowflake"))
    query = {
        "version": 2,
        "select": [
            {
                "as": "inventory_from_ordering_stores_share",
                "expression": {
                    "kind": "ratio",
                    "numerator": {
                        "kind": "scoped_aggregate",
                        "measure": "measure.jaffle.inventory_on_hand_eop",
                        "aggregation": "sum",
                        "predicates": [
                            {
                                "measure": "measure.jaffle.order_count",
                                "entity": "entity.jaffle_store",
                                "op": ">",
                                "value": 0,
                            }
                        ],
                    },
                    "denominator": {
                        "kind": "scoped_aggregate",
                        "measure": "measure.jaffle.inventory_on_hand_eop",
                        "aggregation": "sum",
                    },
                },
            }
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {
            "temporal_role": "temporal_role.jaffle_inventory_day",
            "grain": "month",
            "start": "2024-01-01",
            "end": "2024-02-01",
        },
    }

    compiled = compile_query(config, Registry(config), query)
    rendered = compiled["sql"]

    assert "QUALIFY" in rendered
    assert "ROW_NUMBER() OVER" in rendered
    assert "latest_jaffle_store_inventory_snapshot_snapshot_complete" not in rendered
    assert any(
        row["kind"] == "snapshot_row_selection" and row["strategy"] == "qualify_row_number"
        for row in compiled["physical_plan"].optimizations
    )


def test_distribution_rolls_snapshot_to_entity_before_percentile(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = {
        "version": 2,
        "select": [
            {
                "as": "p80_inventory",
                "expression": {
                    "kind": "distribution",
                    "function": "percentile",
                    "p": 0.8,
                    "over": {
                        "kind": "entity_value",
                        "entity": "entity.jaffle_store",
                        "input": {
                            "measure": "measure.jaffle.inventory_on_hand_eop",
                            "aggregation": "sum",
                        },
                        "where": [{"kind": "value_filter", "op": ">", "value": 0}],
                    },
                },
            }
        ],
        "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
    }

    compiled = compile_query(config, Registry(config), query)
    rendered = compiled["sql"]

    assert "p80_inventory__entity_values" in rendered
    assert 'base.g1 AS "dimension.jaffle_store_id"' in rendered
    assert "__entity_value > 0" in rendered
    assert "QUANTILE_CONT(" in rendered
    assert any(row.kind == "EntityRollup" for row in compiled["logical_plan"].semantic_dag.nodes)
    assert any(
        row.kind == "DistributionAggregate" for row in compiled["logical_plan"].semantic_dag.nodes
    )


def test_metric_time_alignment_does_not_mask_single_leaf_temporal_errors(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    updated_measures = []
    for measure in config.measures:
        if measure.id == "measure.jaffle.item_revenue_usd":
            updated_measures.append(
                replace(
                    measure,
                    compatible_temporal_roles=[
                        "temporal_role.jaffle_order_time",
                        "temporal_role.jaffle_lifecycle_delivered_at",
                    ],
                )
            )
        else:
            updated_measures.append(measure)
    config = replace(config, measures=updated_measures)

    with pytest.raises(SemanticLayerError) as exc:
        compile_query(
            config,
            Registry(config),
            {
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.item_revenue_usd",
                            "aggregation": "sum",
                        },
                        "as": "item_revenue_usd",
                    }
                ],
                "time": {"temporal_role": "temporal_role.jaffle_store_opened_at", "grain": "month"},
            },
        )

    assert exc.value.code == "INCOMPATIBLE_TEMPORAL_ROLE"


def test_semi_additive_snapshots_choose_period_row_before_summing(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = {
        "select": [
            {
                "expression": {"metric": "metric.sales.inventory_on_hand_eop"},
                "as": "inventory_on_hand_eop",
            },
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
    }

    rendered = compile_query(config, Registry(config), query)["sql"]

    assert "snapshot_complete AS (" in rendered
    assert "MAX(leaf_1__snapshot_base.__snapshot_order) AS __snapshot_order" in rendered
    assert "SUM(leaf_1__snapshot_base.__snapshot_value) AS m1" in rendered
    assert "arg_max" not in rendered.lower()


def test_advanced_time_sql_supports_calendar_dense_fill_and_period_rewrites(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = {
        "select": [
            {
                "expression": {
                    "kind": "prior_period",
                    "input": {"measure": "measure.jaffle.order_count"},
                    "offset": {"unit": "year", "value": 1},
                },
                "as": "prior_year_orders",
            },
            {
                "expression": {
                    "kind": "period_to_date",
                    "input": {"measure": "measure.jaffle.order_count"},
                    "period": "quarter",
                },
                "as": "orders_qtd",
            },
        ],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "month",
            "calendar_id": "default",
        },
    }

    compiled = compile_query(config, Registry(config), query)
    rendered = compiled["sql"]

    assert "FROM jaffle_calendar" in rendered
    assert "LAG(base.m1, 12)" in rendered
    assert "DATE_TRUNC('quarter'" in rendered


def test_native_percentile_and_median_aggregations_compile(package_config_factory):
    jaffle_config, _ = package_config_factory("jaffle_shop")

    median_query = {
        "select": [
            {
                "expression": {
                    "measure": "measure.jaffle.item_revenue_usd",
                    "aggregation": "median",
                },
                "as": "median_item_revenue",
            }
        ],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    }
    percentile_query = {
        "select": [
            {
                "expression": {
                    "measure": "measure.jaffle.item_revenue_usd",
                    "aggregation": "percentile",
                    "parameters": {"p": 0.95},
                },
                "as": "p95_item_revenue",
            }
        ],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    }

    median_sql = compile_query(jaffle_config, Registry(jaffle_config), median_query)["sql"]
    percentile_sql = compile_query(jaffle_config, Registry(jaffle_config), percentile_query)["sql"]

    assert "MEDIAN((" in median_sql or "MEDIAN(" in median_sql
    assert "QUANTILE_CONT(" in percentile_sql


def test_snowflake_percentile_uses_within_group_syntax(package_config_factory):
    jaffle_config, _ = package_config_factory("jaffle_shop")
    snowflake_config = replace(
        jaffle_config, package=replace(jaffle_config.package, warehouse="snowflake")
    )
    percentile_query = {
        "select": [
            {
                "expression": {
                    "measure": "measure.jaffle.item_revenue_usd",
                    "aggregation": "percentile",
                    "parameters": {"p": 0.95},
                },
                "as": "p95_item_revenue",
            }
        ],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    }

    percentile_sql = compile_query(snowflake_config, Registry(snowflake_config), percentile_query)[
        "sql"
    ]

    assert "PERCENTILE_CONT(0.95) WITHIN GROUP" in percentile_sql
    assert "QUANTILE_CONT(" not in percentile_sql


def test_cumulative_metrics_reject_bounded_start_windows(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {"metric": "metric.sales.cumulative_revenue"},
                        "as": "cumulative_revenue",
                    },
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "month",
                    "start": "2017-01-01 00:00:00",
                    "end": "2017-04-01 00:00:00",
                },
            }
        )
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "CUMULATIVE_TIME_FILTER_UNSUPPORTED"
    finally:
        runtime.close()


def test_metric_predicate_is_not_selectable_and_repeat_customer_orders_compile(
    package_config_factory, runtime_factory
):
    config, _ = package_config_factory("jaffle_shop")

    compiled = compile_query(
        config,
        Registry(config),
        {
            "select": [
                {
                    "expression": {"metric": "metric.sales.repeat_customer_orders"},
                    "as": "repeat_customer_orders",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        },
    )

    assert "qualified_customers_by_lifetime_order_count_1" in compiled["sql"]
    assert "INNER JOIN leaf_1__qualified_customers_by_lifetime_order_count_1" in compiled["sql"]

    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": [
                    {
                        "expression": {
                            "kind": "metric_predicate",
                            "entity": "entity.jaffle_customer",
                            "input": {"measure": "measure.jaffle.lifetime_order_count"},
                            "op": ">",
                            "value": 1,
                        },
                        "as": "repeat_customers_only",
                    }
                ]
            }
        )
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "INVALID_METRIC_PREDICATE"
    finally:
        runtime.close()


def test_contextual_metric_predicates_inherit_time_and_minimal_context_entities(
    package_config_factory,
):
    config, _ = package_config_factory("jaffle_shop")
    qualified_orders_expr = {
        "kind": "scoped_aggregate",
        "measure": "measure.jaffle.order_count",
        "aggregation": "count_distinct",
        "predicates": [
            {
                "measure": "measure.jaffle.order_count",
                "entity": "entity.jaffle_customer",
                "op": ">",
                "value": 10,
            }
        ],
    }

    store_compiled = compile_query(
        config,
        Registry(config),
        {
            "select": [
                {
                    "expression": qualified_orders_expr,
                    "as": "orders",
                }
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        },
    )
    store_sql = store_compiled["sql"]

    assert '"dimension.jaffle_customer_id"' in store_sql
    assert '"dimension.jaffle_store_id"' in store_sql
    assert '"temporal_role.jaffle_order_time__month"' in store_sql
    assert (
        'jaffle_store.store_id = leaf_1__qualified_customers_month_by_order_count_1."dimension.jaffle_store_id"'
        in store_sql
    )

    customer_attr_compiled = compile_query(
        config,
        Registry(config),
        {
            "select": [
                {
                    "expression": qualified_orders_expr,
                    "as": "orders",
                }
            ],
            "group_by": ["dimension.jaffle_customer_type"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        },
    )
    predicate_source_sql = customer_attr_compiled["sql"].split(
        "qualified_customers_month_by_order_count_1 AS (", 1
    )[0]

    assert '"dimension.jaffle_customer_id"' in predicate_source_sql
    assert "base.t AS t" in predicate_source_sql
    assert '"dimension.jaffle_customer_type"' not in predicate_source_sql


def test_contextual_metric_predicates_support_coarser_ancestor_grains_and_reject_invalid_mixes(
    runtime_factory,
):
    runtime = runtime_factory("jaffle_shop")
    try:
        coarse = runtime.validate(
            {
                "select": [
                    {
                        "expression": {
                            "kind": "scoped_aggregate",
                            "measure": "measure.jaffle.order_count",
                            "aggregation": "count_distinct",
                            "predicates": [
                                {
                                    "measure": "measure.jaffle.order_count",
                                    "entity": "entity.jaffle_customer",
                                    "op": ">",
                                    "value": 10,
                                    "time_grain": "month",
                                }
                            ],
                        },
                        "as": "orders",
                    }
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
            }
        )
        finer = runtime.validate(
            {
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
                "metric_filters": [
                    {
                        "expression": {
                            "kind": "metric_predicate",
                            "entity": "entity.jaffle_customer",
                            "scope_mode": "contextual",
                            "time_grain": "day",
                            "input": {"measure": "measure.jaffle.order_count"},
                            "op": ">",
                            "value": 1,
                        },
                        "op": "=",
                        "value": True,
                    }
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            }
        )
        misaligned = runtime.validate(
            {
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
                "metric_filters": [
                    {
                        "expression": {
                            "kind": "metric_predicate",
                            "entity": "entity.jaffle_customer",
                            "scope_mode": "contextual",
                            "time_grain": "month",
                            "input": {"measure": "measure.jaffle.order_count"},
                            "op": ">",
                            "value": 1,
                        },
                        "op": "=",
                        "value": True,
                    }
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "week"},
            }
        )

        assert coarse["ok"] is True
        assert finer["ok"] is False
        assert finer["errors"][0]["code"] == "PREDICATE_GRAIN_UNSAFE"
        assert misaligned["ok"] is False
        assert misaligned["errors"][0]["code"] == "PREDICATE_GRAIN_UNSAFE"
    finally:
        runtime.close()


def test_conversion_metrics_compile_with_supported_event_pair_semantics(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": [
                    {
                        "expression": {
                            "metric": "metric.sales.session_to_order_conversion_rate_7d"
                        },
                        "as": "conversion_rate",
                    }
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_session_started_at",
                    "grain": "day",
                },
            }
        )
        assert report["ok"] is True
        rendered = report["explain"]["rendered_sql"]
        assert "ROW_NUMBER()" in rendered
        assert "DATE_DIFF('day'" in rendered
    finally:
        runtime.close()


def test_conversion_with_non_event_measure_raises_conversion_not_supported(runtime_factory):
    # revenue_usd is a flow aggregate (not event_count / distinct_population),
    # so the planner must reject it as a conversion base/converted input.
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": [
                    {
                        "expression": {
                            "kind": "conversion",
                            "entity": "entity.jaffle_customer",
                            "window": {"unit": "day", "value": 7},
                            "matching_mode": "first_converted_after_base",
                            "base": {"kind": "aggregate", "measure": "measure.jaffle.revenue_usd"},
                            "converted": {
                                "kind": "aggregate",
                                "measure": "measure.jaffle.revenue_usd",
                            },
                        },
                        "as": "bad_conversion",
                    }
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
            }
        )
        assert report["ok"] is False
        assert any(err["code"] == "CONVERSION_NOT_SUPPORTED" for err in report["errors"]), report[
            "errors"
        ]
    finally:
        runtime.close()


def test_conversion_by_converted_side_product_type_uses_bound_denominator_policy(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": [
                    {
                        "expression": {
                            "metric": "metric.adoption.signup_to_send_conversion_rate_28d"
                        },
                        "as": "rate",
                    }
                ],
                "group_by": ["dimension.jaffle_product_type"],
                "time": {
                    "temporal_role": "temporal_role.jaffle_session_started_at",
                    "grain": "month",
                },
            }
        )
        assert report["ok"] is True
        rendered = report["explain"]["rendered_sql"]
        assert "conversion_denominator" in rendered
        assert "conversion_numerator" in rendered
        assert "RANK() OVER" in rendered
        assert "INNER JOIN jaffle_item ON jaffle_order.order_id = jaffle_item.order_id" in rendered
        assert "INNER JOIN jaffle_product ON jaffle_item.sku = jaffle_product.sku" in rendered

        result = runtime.query(
            {
                "select": [
                    {
                        "expression": {
                            "metric": "metric.adoption.signup_to_send_conversion_rate_28d"
                        },
                        "as": "rate",
                    }
                ],
                "group_by": ["dimension.jaffle_product_type"],
                "time": {
                    "temporal_role": "temporal_role.jaffle_session_started_at",
                    "grain": "month",
                },
                "order_by": [
                    {"field": "time", "direction": "ASC"},
                    {"field": "dimension.jaffle_product_type", "direction": "ASC"},
                ],
            }
        )
        assert result["status"] == "ok"
        assert result["row_count"] > 0
        assert all(row["dimension.jaffle_product_type"] is not None for row in result["rows"])
    finally:
        runtime.close()


def test_conversion_by_converted_side_product_type_requires_denominator_policy(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": [
                    {
                        "expression": {
                            "kind": "conversion",
                            "entity": "entity.jaffle_customer",
                            "window": {"unit": "day", "value": 28},
                            "matching_mode": "first_converted_after_base",
                            "base": {
                                "kind": "aggregate",
                                "measure": "measure.jaffle.session_starts",
                            },
                            "converted": {
                                "kind": "aggregate",
                                "measure": "measure.jaffle.order_count",
                            },
                        },
                        "as": "rate",
                    }
                ],
                "group_by": ["dimension.jaffle_product_type"],
                "time": {
                    "temporal_role": "temporal_role.jaffle_session_started_at",
                    "grain": "month",
                },
            }
        )
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "CONVERSION_NOT_SUPPORTED"
        assert report["errors"][0]["details"]["required_binding"] == {
            "side": "converted",
            "denominator": "all_base_events",
        }
    finally:
        runtime.close()


def test_same_store_conversion_with_mismatched_time_block_fails_validate(runtime_factory):
    """Regression: reviewer scenario where the same-store 7d conversion
    metric (anchored on `temporal_role.jaffle_session_started_at`) was
    queried with `time.temporal_role = temporal_role.jaffle_order_time`.

    Previously `validate` and `compile` passed and only `execute` failed
    with a DuckDB binder error: the conversion-leaf CTE selects from the
    base anchor (session) relation but the time filter referenced
    ``jaffle_order``. The pre-validation gate must now reject this with
    a structured ``INVALID_TEMPORAL_BINDING`` envelope before any SQL is
    rendered.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": [
                    {
                        "expression": {
                            "metric": "metric.sales.session_to_order_conversion_rate_7d_same_store"
                        },
                        "as": "rate",
                    }
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
            }
        )
        assert report["ok"] is False, report
        codes = [err["code"] for err in report["errors"]]
        assert "INVALID_TEMPORAL_BINDING" in codes, codes
        envelope = next(
            err for err in report["errors"] if err["code"] == "INVALID_TEMPORAL_BINDING"
        )
        details = envelope.get("details", {})
        assert details.get("anchor_temporal_role") == "temporal_role.jaffle_session_started_at"
        assert "temporal_role.jaffle_session_started_at" in details.get(
            "allowed_temporal_roles", []
        )
        # Compile must surface the same envelope, not silently render SQL.
        with pytest.raises(SemanticLayerError) as exc:
            runtime.compile(
                {
                    "select": [
                        {
                            "expression": {
                                "metric": "metric.sales.session_to_order_conversion_rate_7d_same_store"
                            },
                            "as": "rate",
                        }
                    ],
                    "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
                }
            )
        assert exc.value.code == "INVALID_TEMPORAL_BINDING"
        # The recovery_hints path must surface the anchor suggestion.
        from semantic_rails.diagnostics import recovery_hints_for_error

        hints = recovery_hints_for_error(exc.value.code, exc.value.details)
        kinds = {h.get("kind") for h in hints}
        assert "filter_on_conversion_anchor" in kinds
    finally:
        runtime.close()


def test_same_store_conversion_with_anchor_aligned_time_block_executes(runtime_factory):
    """Positive: when the time block targets the conversion's declared
    anchor temporal role (``jaffle_session_started_at``), the same-store
    conversion compiles, executes, and returns rows.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = {
            "select": [
                {
                    "expression": {
                        "metric": "metric.sales.session_to_order_conversion_rate_7d_same_store"
                    },
                    "as": "rate",
                }
            ],
            "time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "day"},
        }
        report = runtime.validate(payload)
        assert report["ok"] is True, report
        result = runtime.query(payload)
        assert result["status"] == "ok"
        assert result["row_count"] >= 1
    finally:
        runtime.close()


def test_session_to_order_conversion_rate_corpus_case_still_compiles(runtime_factory):
    """Positive regression: the corpus benchmark case (anchor-aligned
    session→order conversion) keeps validating and executing — this
    proves the new pre-validation gate doesn't over-reject."""
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = {
            "select": [
                {
                    "expression": {"metric": "metric.sales.session_to_order_conversion_rate_7d"},
                    "as": "conversion_rate",
                }
            ],
            "time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "day"},
        }
        report = runtime.validate(payload)
        assert report["ok"] is True, report
        result = runtime.query(payload)
        assert result["status"] == "ok"
    finally:
        runtime.close()


def _predicate_filter_query(*, predicate: dict, **extra) -> dict:
    """Build a runtime.validate payload that exercises a metric_predicate
    via the metric_filters path on top of a simple order_count select."""
    payload: dict = {
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
        "metric_filters": [{"expression": predicate, "op": "=", "value": True}],
    }
    payload.update(extra)
    return payload


def test_metric_predicate_subquery_skips_recursive_top_level_compile(
    package_config_factory, monkeypatch
):
    config, _ = package_config_factory("jaffle_shop")
    original_compile_query = compiler_module.compile_query
    compile_calls = 0

    def counted_compile_query(*args, **kwargs):
        nonlocal compile_calls
        compile_calls += 1
        if compile_calls > 1:
            raise AssertionError(
                "metric predicate subqueries should use the AST-only compiler path"
            )
        return original_compile_query(*args, **kwargs)

    monkeypatch.setattr(compiler_module, "compile_query", counted_compile_query)
    compiled = compiler_module.compile_query(
        config,
        Registry(config),
        _predicate_filter_query(
            predicate={
                "kind": "metric_predicate",
                "entity": "entity.jaffle_customer",
                "scope_mode": "entity_only",
                "input": {"measure": "measure.jaffle.order_count"},
                "op": ">",
                "value": 1,
            }
        ),
    )

    assert compile_calls == 1
    assert "qualified_customers" in compiled["sql"]


def test_metric_predicate_with_literal_input_raises_predicate_not_supported(runtime_factory):
    # paths.py:159 — _expression_root_entity falls through for kinds it does
    # not recognize as predicate-planning inputs (e.g. a bare literal).
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            _predicate_filter_query(
                predicate={
                    "kind": "metric_predicate",
                    "entity": "entity.jaffle_customer",
                    "scope_mode": "entity_only",
                    "input": {"kind": "literal", "value": 1},
                    "op": ">",
                    "value": 0,
                }
            )
        )
        assert report["ok"] is False
        codes = [err["code"] for err in report["errors"]]
        assert "PREDICATE_NOT_SUPPORTED" in codes, codes
    finally:
        runtime.close()


def test_metric_predicate_without_time_anchor_through_temporal_path_raises_predicate_scope_unsafe(
    runtime_factory,
):
    # compiler.py:902 — predicate.entity != input_root, the chosen path
    # crosses a temporal_validity relationship (orders → customer_history),
    # but the query supplies no time anchor, so the planner cannot pick
    # an SCD2 slice. Expected: PREDICATE_SCOPE_UNSAFE.
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            _predicate_filter_query(
                predicate={
                    "kind": "metric_predicate",
                    "entity": "entity.jaffle_customer_history",
                    "scope_mode": "entity_only",
                    "input": {"measure": "measure.jaffle.order_count"},
                    "op": ">",
                    "value": 0,
                }
            )
        )
        assert report["ok"] is False
        codes = [err["code"] for err in report["errors"]]
        assert "PREDICATE_SCOPE_UNSAFE" in codes, codes
    finally:
        runtime.close()


def test_metric_predicate_filter_through_temporal_path_without_time_raises_predicate_filter_incompatible(
    runtime_factory,
):
    # compiler.py:954 — contextual predicate with a where-filter on a
    # dimension reachable only through a temporal_validity path, but the
    # query supplies no time anchor. Expected: PREDICATE_FILTER_INCOMPATIBLE.
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            _predicate_filter_query(
                predicate={
                    "kind": "metric_predicate",
                    "entity": "entity.jaffle_customer",
                    "scope_mode": "contextual",
                    "input": {"measure": "measure.jaffle.order_count"},
                    "op": ">",
                    "value": 0,
                },
                where=[
                    {
                        "field": "dimension.jaffle_customer_history_segment",
                        "op": "=",
                        "value": "high_value",
                    }
                ],
            )
        )
        assert report["ok"] is False
        codes = [err["code"] for err in report["errors"]]
        assert "PREDICATE_FILTER_INCOMPATIBLE" in codes, codes
    finally:
        runtime.close()


def test_out_of_scope_question_with_enforce_scope_raises_out_of_scope(runtime_factory):
    # runtime.py:124 — when payload sets enforce_scope: true and the
    # question is classified as non-data-query (here: diagnostic_synthesis),
    # validate must refuse with OUT_OF_SCOPE before compilation.
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "select": [
                    {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "revenue"}
                ],
                "enforce_scope": True,
                "question": "Why did revenue decline last quarter?",
            }
        )
        assert report["ok"] is False
        codes = [err["code"] for err in report["errors"]]
        assert "OUT_OF_SCOPE" in codes, codes
    finally:
        runtime.close()
