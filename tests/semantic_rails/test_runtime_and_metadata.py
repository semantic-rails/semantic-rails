from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from semantic_rails import config as config_module
from semantic_rails.metadata import (
    build_options_payload,
    catalog_payload,
    discover_payload,
    inspect_payload,
    valid_values_payload,
)
from semantic_rails.runtime import Runtime
from tests.plan_candidate_envelope import plan_candidate_envelope


def _fake_query_result(payload):
    group_by = list(payload.get("group_by", []) or [])
    dimension = group_by[0] if group_by else "dimension.jaffle_store_name"
    return {
        "rows": [
            {
                dimension: "Brooklyn",
                "orders": 42,
                "aov_usd": 19.5,
            }
        ],
        "row_count": 1,
        "rendered_sql": "select 1",
        "logical_plan": {},
        "sql_plan": {},
        "explain": {},
        "query": dict(payload),
        "normalized_query": {},
    }


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_operational_package(package_dir: Path) -> None:
    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": "operational_demo",
                "name": "operational_demo",
                "description": "Operational metadata demo",
                "default_db": "demo.duckdb",
                "seed": {"kind": "sql_script", "source": "data/seed_example.sql", "post_sql": ""},
            },
            "defaults": {
                "time": {
                    "timezone": "UTC",
                    "default_query_axis": False,
                    "supported_grains": ["day", "week", "month", "quarter", "year"],
                },
                "operational": {
                    "measure": {
                        "fields": {
                            "owner": {"type": "string", "required": True},
                            "tags": {"type": "string_list", "required": False},
                        }
                    },
                    "metric": {
                        "fields": {
                            "owner": {"type": "string", "required": True},
                            "tags": {"type": "string_list", "required": False},
                            "verified": {"type": "boolean", "required": False},
                        }
                    },
                },
            },
        },
    )
    _write_yaml(
        package_dir / "graph.yml",
        {
            "graph": {
                "entities": {
                    "order": {
                        "id": "entity.demo_order",
                        "name": "demo.Order",
                        "label": "Order",
                        "key": ["order_id"],
                        "model": "orders",
                    }
                }
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "orders.yml",
        {
            "model": {
                "id": "orders",
                "entity": "order",
                "relation": "order_fact",
                "grain": ["order_id"],
                "operational_defaults": {"owner": "finance"},
                "times": {
                    "ordered_at": {
                        "id": "temporal_role.demo_order_time",
                        "name": "demo.Order.ordered_at",
                        "label": "Order time",
                        "column": "ordered_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    }
                },
                "measures": {
                    "order_count": {
                        "id": "measure.demo.order_count",
                        "name": "sales.orders",
                        "label": "Orders",
                        "kind": "entity_count",
                        "time": "ordered_at",
                        "operational": {"tags": ["core"]},
                        "publish": {
                            "id": "metric.sales.orders",
                            "operational": {"verified": True},
                        },
                    }
                },
            }
        },
    )
    _write_yaml(
        package_dir / "metrics.yml",
        {
            "metrics": {
                "sales.orders_copy": {
                    "id": "metric.sales.orders_copy",
                    "name": "sales.orders_copy",
                    "label": "Orders copy",
                    "kind": "derived",
                    "temporal_role": "temporal_role.demo_order_time",
                    "expression": {"kind": "aggregate", "measure": "measure.demo.order_count"},
                    "operational": {"owner": "curated", "verified": False},
                }
            }
        },
    )


def _write_generic_planning_package(package_dir: Path) -> None:
    _write_yaml(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": "generic_planning_demo",
                "name": "generic_planning_demo",
                "description": "Generic planning scoring demo",
                "default_db": "demo.duckdb",
                "seed": {"kind": "sql_script", "source": "data/seed.sql", "post_sql": ""},
            },
            "defaults": {
                "time": {
                    "timezone": "UTC",
                    "default_query_axis": False,
                    "supported_grains": ["day", "week", "month", "quarter", "year"],
                },
            },
        },
    )
    _write_yaml(
        package_dir / "graph.yml",
        {
            "graph": {
                "entities": {
                    "event": {
                        "id": "entity.demo_event",
                        "name": "demo.Event",
                        "label": "Event",
                        "key": ["event_id"],
                        "model": "events",
                    }
                }
            }
        },
    )
    _write_yaml(
        package_dir / "models" / "events.yml",
        {
            "model": {
                "id": "events",
                "entity": "event",
                "relation": "demo_events",
                "grain": ["event_id"],
                "times": {
                    "event_at": {
                        "id": "temporal_role.demo_event_time",
                        "name": "demo.Event.event_at",
                        "label": "Event time",
                        "column": "event_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "default_query_axis": True,
                    }
                },
                "dimensions": {
                    "product": {
                        "id": "dimension.demo_product",
                        "name": "demo.Event.product",
                        "label": "Product",
                        "column": "product",
                        "kind": "categorical",
                    },
                    "send_type": {
                        "id": "dimension.demo_send_type",
                        "name": "demo.Event.send_type",
                        "label": "Send type",
                        "column": "send_type",
                        "kind": "categorical",
                    },
                    "account_status": {
                        "id": "dimension.demo_account_status",
                        "name": "demo.Event.account_status",
                        "label": "Account status",
                        "column": "account_status",
                        "kind": "categorical",
                        "domain": [
                            {"value": "paying", "label": "Paying", "aliases": ["paid"]},
                            {"value": "free", "label": "Free"},
                        ],
                    },
                },
                "measures": {
                    "send_count": {
                        "id": "measure.demo.send_count",
                        "name": "engagement.sends",
                        "label": "Sends",
                        "kind": "entity_count",
                        "entity_key": "event_id",
                        "time": "event_at",
                        "publish": {"id": "metric.engagement.sends"},
                    },
                    "capacity": {
                        "id": "measure.demo.capacity",
                        "name": "engagement.capacity",
                        "label": "Capacity",
                        "expr": "capacity",
                        "default_aggregation": "sum",
                        "allowed_aggregations": ["sum"],
                        "time": "event_at",
                        "publish": {"id": "metric.engagement.capacity"},
                    },
                    "account_count": {
                        "id": "measure.demo.account_count",
                        "name": "engagement.accounts",
                        "label": "Accounts",
                        "kind": "entity_count",
                        "entity_key": "account_id",
                        "time": "event_at",
                        "publish": {"id": "metric.engagement.accounts"},
                    },
                },
            }
        },
    )
    _write_yaml(
        package_dir / "metrics.yml",
        {
            "metrics": {
                "engagement.utilization_rate": {
                    "id": "metric.engagement.utilization_rate",
                    "name": "engagement.utilization_rate",
                    "label": "Utilization rate",
                    "description": "Sends divided by available capacity.",
                    "temporal_role": "temporal_role.demo_event_time",
                    "expression": {
                        "kind": "binary",
                        "op": "divide",
                        "left": {"kind": "metric", "metric": "metric.engagement.sends"},
                        "right": {"kind": "metric", "metric": "metric.engagement.capacity"},
                    },
                },
                "engagement.sends_per_account": {
                    "id": "metric.engagement.sends_per_account",
                    "name": "engagement.sends_per_account",
                    "label": "Sends per account",
                    "description": "Sends divided by account count.",
                    "temporal_role": "temporal_role.demo_event_time",
                    "expression": {
                        "kind": "binary",
                        "op": "divide",
                        "left": {"kind": "metric", "metric": "metric.engagement.sends"},
                        "right": {"kind": "metric", "metric": "metric.engagement.accounts"},
                    },
                },
            }
        },
    )


def test_validate_compile_and_query_happy_path_on_jaffle_shop(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    runtime.query = _fake_query_result
    try:
        # `verbosity="full"` keeps `physical_plan` on the compile envelope
        # — the new compact default drops it.
        query = {
            "version": 1,
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
            "verbosity": "full",
        }

        validated = runtime.validate(query)
        compiled = runtime.compile(query)
        result = runtime.query(query)

        assert validated["ok"] is True
        assert validated["query"] == query
        assert validated["normalized_query"]["group_by"] == ["dimension.jaffle_store_name"]
        assert (
            validated["normalized_query"]["time"]["temporal_role"]
            == "temporal_role.jaffle_order_time"
        )
        assert validated["logical_plan"]["root_entity"] == "entity.jaffle_order"
        # `projected` passthrough CTE is inlined when there are no metric filters.
        assert "projected AS (" not in compiled["rendered_sql"]
        assert "AS aov_usd" in compiled["rendered_sql"]
        assert compiled["physical_plan"]["nodes"]
        assert compiled["query"] == query
        assert result["row_count"] > 0
        assert "dimension.jaffle_store_name" in result["rows"][0]
        assert result["rows"][0]["orders"] >= 0
        assert result["rows"][0]["aov_usd"] >= 0
    finally:
        runtime.close()


def test_validate_uses_compiled_plan_cache(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # `verbosity="full"` keeps `compile_stats` at the top level —
        # the new compact default drops it.
        query = {
            "version": 1,
            "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 3,
            "verbosity": "full",
        }

        first = runtime.validate(query)
        second = runtime.validate(query)

        assert first["ok"] is True
        assert first["compile_stats"]["cache_hit"] is False
        assert second["ok"] is True
        assert second["compile_stats"]["cache_hit"] is True
        assert second["explain"]["physical_plan"]["nodes"]
    finally:
        runtime.close()


def test_compile_cache_is_not_mutated_by_returned_compilation(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        query = {
            "version": 1,
            "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 3,
        }

        first = runtime._compile(query, policy_context={})
        original_sql = first["sql"]
        first["sql"] = "select mutated"
        first["logical_plan"].measure_plans.clear()

        second = runtime._compile(query, policy_context={})

        assert second["compile_stats"]["cache_hit"] is True
        assert second["sql"] == original_sql
        assert second["logical_plan"].measure_plans
    finally:
        runtime.close()


def test_runtime_cached_responses_are_defensive_copies(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        catalog = runtime.catalog()
        catalog["package"]["name"] = "mutated"
        catalog["objects"][0]["id"] = "mutated"

        resolved = runtime.resolve("median_item_revenue", kind="metric")
        resolved["object"]["payload"]["compatible_temporal_roles"].append("mutated")

        assert runtime.catalog()["package"]["name"] == "jaffle_shop"
        assert runtime.catalog()["objects"][0]["id"] != "mutated"
        assert (
            "mutated"
            not in runtime.resolve("median_item_revenue", kind="metric")["object"]["payload"][
                "compatible_temporal_roles"
            ]
        )
    finally:
        runtime.close()


def test_runtime_compile_cache_supports_basic_concurrent_use(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # `verbosity="full"` keeps `compile_stats` at the top level —
        # the new compact default drops it.
        query = {
            "version": 1,
            "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 3,
            "verbosity": "full",
        }

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: runtime.validate(query), range(16)))

        assert all(result["ok"] is True for result in results)
        assert any(result["compile_stats"]["cache_hit"] is True for result in results)
    finally:
        runtime.close()


def test_valid_values_supports_value_domains_and_filtered_duckdb(runtime_factory):
    jaffle = runtime_factory("jaffle_shop")
    jaffle.query = _fake_query_result
    try:
        domain_values = valid_values_payload(jaffle, dimension_id="dimension.jaffle_store_name")
        assert domain_values["source"] == "value_domain"
        assert domain_values["selection"]["dimension_id"] == "dimension.jaffle_store_name"
        assert domain_values["value_domain_id"] == "value_domain.jaffle_store_store_name"
        assert [row["value"] for row in domain_values["values"]] == [
            "Brooklyn",
            "Chicago",
            "New Orleans",
            "Philadelphia",
            "San Francisco",
        ]

        filtered = valid_values_payload(
            jaffle,
            dimension_id="dimension.jaffle_order_customer_id",
            query={
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
                "where": [
                    {
                        "field": "dimension.jaffle_order_ordered_at",
                        "op": ">=",
                        "value": "2016-09-01T00:00:00Z",
                    }
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            },
            allow_live_query=True,
        )
        assert filtered["source"] == "duckdb"
        assert filtered["query_state"]["normalized_query"]["group_by"] == [
            "dimension.jaffle_order_customer_id"
        ]
        assert filtered["values"]
        assert all("value" in row and "label" in row for row in filtered["values"])
    finally:
        jaffle.close()


def test_metadata_cards_catalog_and_build_options_do_not_live_query_values(runtime_factory):
    runtime = runtime_factory("jaffle_shop")

    def _raise_live_query(*args, **kwargs):
        raise AssertionError("metadata endpoint opened a live query path")

    runtime.query = _raise_live_query
    runtime._get_adapter = _raise_live_query
    try:
        query = {
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
        }

        compact_catalog = catalog_payload(runtime, verbosity="compact")
        full_catalog = catalog_payload(runtime, verbosity="full")
        declared_card = inspect_payload(
            runtime, object_id="dimension.jaffle_item_product_type", partial_query=query
        )["card"]
        open_card = inspect_payload(
            runtime, object_id="dimension.jaffle_order_customer_id", partial_query=query
        )["card"]
        discovered = discover_payload(
            runtime, terms="customer", kinds=["dimension"], partial_query=query, limit=3
        )
        filter_options = build_options_payload(
            runtime,
            partial_query=query,
            step="filter_value",
            focus_object_id="dimension.jaffle_order_customer_id",
            limit=5,
        )

        assert compact_catalog["meta"]["verbosity"] == "compact"
        assert full_catalog["meta"]["verbosity"] == "full"
        assert [row["value"] for row in declared_card["sample_values"]] == ["jaffle", "beverage"]
        assert declared_card["valid_values_lookup_required"] is False
        assert open_card["sample_values"] == []
        assert open_card["top_values"] == []
        assert open_card["valid_values_lookup_required"] is True
        assert "valid-values" in open_card["valid_values_hint"]
        assert discovered["dimensions"]
        assert filter_options["recommended"] == []
        assert filter_options["available"] == []
        assert filter_options["query_patches"] == []
    finally:
        runtime.close()


def test_valid_values_defaults_to_declared_domains_without_live_query(runtime_factory):
    runtime = runtime_factory("jaffle_shop")

    def _raise_live_query(*args, **kwargs):
        raise AssertionError("valid-values should require allow_live_query for warehouse lookup")

    runtime.query = _raise_live_query
    runtime._get_adapter = _raise_live_query
    try:
        query = {
            "version": 1,
            "where": [
                {
                    "field": "dimension.jaffle_order_ordered_date",
                    "op": ">=",
                    "value": "2016-09-01",
                }
            ],
        }

        declared = valid_values_payload(
            runtime, dimension_id="dimension.jaffle_item_product_type", query=query
        )
        empty = valid_values_payload(
            runtime, dimension_id="dimension.jaffle_order_customer_id", query=query
        )

        assert declared["source"] == "value_domain"
        assert declared["value_source_type"] == "declared_domain"
        assert [row["value"] for row in declared["values"]] == ["jaffle", "beverage"]
        assert empty["values"] == []
        assert empty["total_count"] == 0
        assert empty["source"] == "none"
        assert empty["value_source_type"] == "none"
        assert empty["estimated_cost"] == "none"
        assert empty["valid_values_lookup_required"] is True
        assert "allow_live_query=true" in empty["valid_values_hint"]
    finally:
        runtime.close()


def test_catalog_temporal_roles_surface_temporal_class(runtime_factory):
    """Regression test for MEDIUM #1: every entry in catalog.temporal_roles
    used to return temporal_class: None even though models declared
    `class: state_time`, etc. The data was in the YAML; the catalog
    endpoint wasn't propagating it."""
    runtime = runtime_factory("jaffle_shop")
    try:
        catalog = catalog_payload(runtime)
        assert catalog["temporal_roles"], "expected at least one temporal role in catalog"
        # No row should have a falsy temporal_class — every role is
        # declared with one of event_time / calendar_time / as_of_time /
        # state_time.
        valid = {"event_time", "calendar_time", "as_of_time", "state_time"}
        observed = {row.get("temporal_class") for row in catalog["temporal_roles"]}
        assert all(row.get("temporal_class") in valid for row in catalog["temporal_roles"]), (
            f"some temporal roles are missing temporal_class: {observed}"
        )
        # The recently-added `state_time` class must surface end-to-end.
        assert "state_time" in observed
    finally:
        runtime.close()


def test_catalog_payload_groups_objects_by_kind(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # ``verbosity="full"`` is required for ``payload`` inspection —
        # compact (the default) strips it for envelope budget. The
        # grouping / alias-index assertions stay verbosity-agnostic.
        catalog = catalog_payload(runtime, verbosity="full")
        assert "entities" in catalog
        assert "dimensions" in catalog
        assert "temporal_roles" in catalog
        assert "measures" in catalog
        assert any(row["id"] == "entity.jaffle_order" for row in catalog["entities"])
        assert "dimensionjafflestorename" in catalog["alias_index"]
        assert "dimension.jaffle_store_name" in catalog["alias_index"]["dimensionjafflestorename"]
        capability_kinds = {row["kind"] for row in catalog["capabilities"]}
        assert "dense_fill" in capability_kinds
        assert "historical_joins" in capability_kinds
        assert "alternate_calendars" in capability_kinds
        assert "metric_predicates" in capability_kinds
        assert "percentile_metrics" in capability_kinds
        assert "conversion_metrics" in capability_kinds
        # `metric.sales.orders` was a wrapper that got dropped. AOV is the
        # canonical order-rooted governed metric in the curated catalog —
        # it carries the same default temporal role and grouping entities.
        aov = next(row for row in catalog["metrics"] if row["id"] == "metric.sales.aov_usd")
        assert aov["payload"]["default_temporal_role"] == "temporal_role.jaffle_order_time"
        assert any(
            entity["id"] == "entity.jaffle_store"
            for entity in aov["payload"]["valid_grouping_entities"]
        )
        repeat_customer_orders = next(
            row for row in catalog["metrics"] if row["id"] == "metric.sales.repeat_customer_orders"
        )
        assert any(
            entity["id"] == "entity.jaffle_customer_history"
            for entity in repeat_customer_orders["payload"]["disabled_grouping_entities"]
            + repeat_customer_orders["payload"]["valid_grouping_entities"]
        )
    finally:
        runtime.close()


def test_operational_metadata_is_exposed_on_inspect_and_full_catalog_only(
    monkeypatch, tmp_path: Path
):
    package_dir = tmp_path / "operational_demo"
    _write_operational_package(package_dir)
    monkeypatch.setattr(
        config_module, "list_package_paths", lambda: {"operational_demo": str(package_dir)}
    )
    runtime = Runtime("operational_demo")
    try:
        measure_card = inspect_payload(runtime, object_id="measure.demo.order_count")["card"]
        metric_card = inspect_payload(runtime, object_id="metric.sales.orders")["card"]
        compact_catalog = catalog_payload(runtime)
        full_catalog = catalog_payload(runtime, verbosity="full")
        resolved = runtime.resolve("orders", kind="metric")

        assert measure_card["operational"] == {"owner": "finance", "tags": ["core"]}
        assert metric_card["operational"] == {
            "owner": "finance",
            "tags": ["core"],
            "verified": True,
        }
        assert resolved["object"]["payload"]["operational"] == {
            "owner": "finance",
            "tags": ["core"],
            "verified": True,
        }

        compact_measure = next(
            row for row in compact_catalog["measures"] if row["id"] == "measure.demo.order_count"
        )
        compact_metric = next(
            row for row in compact_catalog["metrics"] if row["id"] == "metric.sales.orders"
        )
        assert "operational" not in compact_measure
        assert "operational" not in compact_metric
        # ``payload`` is dropped entirely at compact (it's the largest
        # field at 19KB+/measure).
        assert "payload" not in compact_measure
        assert "payload" not in compact_metric

        full_measure = next(
            row for row in full_catalog["measures"] if row["id"] == "measure.demo.order_count"
        )
        full_metric = next(
            row for row in full_catalog["metrics"] if row["id"] == "metric.sales.orders"
        )
        assert full_measure["operational"] == {"owner": "finance", "tags": ["core"]}
        assert full_metric["operational"] == {
            "owner": "finance",
            "tags": ["core"],
            "verified": True,
        }
        assert full_measure["payload"]["operational"] == {"owner": "finance", "tags": ["core"]}
        assert full_metric["payload"]["operational"] == {
            "owner": "finance",
            "tags": ["core"],
            "verified": True,
        }
    finally:
        runtime.close()


def test_runtime_resolve_enriches_metric_time_metadata(runtime_factory):
    """Resolve enriches the resolved object with default + compatible temporal
    roles. Originally tested against metric.sales.orders, but `orders` is now
    queryable as the underlying measure (metric was a pure aggregate wrapper).
    For governed metrics that survive curation, the test verifies the same
    enrichment behavior — here using `median_item_revenue` (kind: semi_additive)
    which carries explicit time-axis semantics."""
    runtime = runtime_factory("jaffle_shop")
    try:
        # Measures are first-class — resolve "orders" against the measure index.
        orders_measure = runtime.resolve("orders", kind="measure")
        assert orders_measure["object"]["id"] == "measure.jaffle.order_count"
        assert (
            "temporal_role.jaffle_order_time"
            in orders_measure["object"]["payload"]["compatible_temporal_roles"]
        )
        # Governed metric resolution: temporal-aware semi_additive metric.
        median = runtime.resolve("median_item_revenue", kind="metric")
        assert median["object"]["id"] == "metric.sales.median_item_revenue"
        assert (
            median["object"]["payload"]["default_temporal_role"]
            == "temporal_role.jaffle_daily_metric_day"
        )
    finally:
        runtime.close()


def test_discover_surfaces_relevant_measures_metrics_and_dimension_values(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        drinks = discover_payload(runtime, terms="drinks", stage="initial", limit=5)
        assert drinks["measures"]
        assert drinks["metrics"]
        assert drinks["dimension_values"]
        assert drinks["stage"] == "initial"
        assert drinks["measures"][0]["object_type"] == "measure"
        assert any(
            row["id"] in {"measure.jaffle.drink_order_count", "measure.jaffle.drink_revenue_usd"}
            for row in drinks["measures"][:2]
        )
        assert any(row["id"] == "metric.sales.drink_revenue_share" for row in drinks["metrics"])
        assert any(
            row["dimension_id"] == "dimension.jaffle_item_product_type"
            and row["value"] == "beverage"
            for row in drinks["dimension_values"]
        )
    finally:
        runtime.close()


def test_discover_sales_prioritizes_core_revenue_measure(runtime_factory):
    """Searching for `sales` should surface the canonical revenue primitive
    near the top. With auto-publish gone, that primitive is the underlying
    `measure.jaffle.revenue_usd` rather than a wrapper metric of the same
    name."""
    runtime = runtime_factory("jaffle_shop")
    try:
        discovered = discover_payload(runtime, terms="sales", stage="initial", limit=12)
        measure_ids = [row["id"] for row in discovered["measures"]]
        # The canonical revenue measure should appear in the top results.
        assert "measure.jaffle.revenue_usd" in measure_ids[:5], (
            f"expected revenue_usd in top 5 measures, got {measure_ids[:5]!r}"
        )
    finally:
        runtime.close()


def test_inspect_exposes_measure_and_dimension_cards(runtime_factory):
    """Inspect cards expose the right shape for each kind: measure cards
    show aggregation defaults and recommended dimensions; metric cards show
    temporal-role defaults; dimension cards show sample values."""
    runtime = runtime_factory("jaffle_shop")
    try:
        measure_card = inspect_payload(runtime, object_id="measure.jaffle.order_count")["card"]
        # Use the surviving delivered measure (metric was a wrapper of it).
        delivered_measure_card = inspect_payload(
            runtime, object_id="measure.jaffle.delivered_revenue_usd"
        )["card"]
        # Use a surviving governed metric for the metric-card check.
        metric_card = inspect_payload(runtime, object_id="metric.sales.aov_usd")["card"]
        dimension_card = inspect_payload(runtime, object_id="dimension.jaffle_item_product_type")[
            "card"
        ]

        assert measure_card["default_aggregation"] == "count_distinct"
        assert "count_distinct" in measure_card["allowed_aggregations"]
        assert measure_card["usage_summary"]["default_aggregation"] == "count_distinct"
        # ``recommended_dimensions``/``preferred_dimensions`` were dropped
        # with the removal of the ``related_dimensions`` authoring field.
        assert "recommended_dimensions" not in measure_card
        assert "preferred_dimensions" not in measure_card
        assert measure_card["comparison_family"] in {"", "order_volume"}

        # Delivered-revenue measure: anchored to lifecycle_delivered_at.
        assert delivered_measure_card["default_aggregation"] == "sum"

        # Governed metric: aov_usd is a ratio, value_type currency.
        assert metric_card["value_type"] == "currency"

        assert dimension_card["kind_semantics"] == "categorical"
        # `preferred_filter_ops` was dropped in v1 — metadata-only, no planner gating.
        assert dimension_card.get("preferred_filter_ops", []) == []
        assert dimension_card["value_domain_summary"]["value_count"] == 2
        assert dimension_card["sample_values"]
        assert any(row["label"] == "Drink" for row in dimension_card["top_values"])
    finally:
        runtime.close()


def test_plan_exposes_contextual_and_lifetime_metric_predicate_semantics(runtime_factory):
    """Inspect the lifetime-cohort metric (`repeat_customer_orders`) to confirm
    it carries entity_only scope semantics, then plan two queries that
    request a contextual cohort filter (re-evaluated per query window). The
    contextual predicate's input ref may resolve to either a measure or a
    metric depending on which surface the planner scores highest — both
    are valid v1 outcomes."""
    runtime = runtime_factory("jaffle_shop")
    try:
        lifetime_card = inspect_payload(runtime, object_id="metric.sales.repeat_customer_orders")[
            "card"
        ]
        # ``verbosity="full"`` retains the heavy ``validation.logical_plan``
        # payload this test inspects (compact strips it for envelope budget).
        contextual = plan_candidate_envelope(
            runtime,
            intent="monthly order volume by store for customers that made more than 10 purchases in that month",
            limit=2,
            verbosity="full",
        )
        daily_contextual = plan_candidate_envelope(
            runtime,
            intent="daily order volume from customers who made more than 10 purchases in that month",
            limit=2,
            verbosity="full",
        )

        assert lifetime_card["predicate_scope_mode"] == "entity_only"
        assert lifetime_card["predicate_entity"] == "entity.jaffle_customer"
        assert lifetime_card["predicate_time_grain"] == ""
        assert lifetime_card["predicate_context_entities"] == []
        assert lifetime_card["predicate_filter_inheritance"] == "none"

        assert contextual["candidates"]
        contextual_query = contextual["candidates"][0]["candidate_ir"]
        contextual_expr = contextual_query["select"][0]["expression"]
        contextual_predicate = contextual_expr["predicates"][0]
        contextual_bound = contextual["candidates"][0]["validation"]["logical_plan"][
            "bound_measures"
        ][0]["filter_spec"]["all"][0]["expression"]
        assert contextual_expr["kind"] == "scoped_aggregate"
        # Predicate carries the entity, op, value — but its input ref may
        # be a measure OR a metric, depending on what the planner picked
        # for the "more than 10 purchases" term. v1 measures are first-class
        # query inputs, so both shapes are acceptable.
        assert contextual_predicate["entity"] == "entity.jaffle_customer"
        assert contextual_predicate["op"] == ">"
        assert contextual_predicate["value"] == 10
        assert "measure" in contextual_predicate or "metric" in contextual_predicate, (
            f"predicate must carry a measure or metric input ref, got {contextual_predicate!r}"
        )
        assert contextual_bound["scope_mode"] == "contextual"
        assert contextual_bound["entity"] == "entity.jaffle_customer"
        assert contextual_query["group_by"] == ["dimension.jaffle_store_name"]

        assert daily_contextual["candidates"]
        daily_query = daily_contextual["candidates"][0]["candidate_ir"]
        daily_predicate = daily_query["select"][0]["expression"]["predicates"][0]
        assert daily_query["time"]["grain"] == "day"
        assert daily_predicate["time_grain"] == "month"
    finally:
        runtime.close()


def test_plan_acceptance_intents_return_ranked_valid_candidates(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    intents = [
        "revenue by store last month",
        "monthly average order value by customer type",
        "food vs drink revenue share by month",
        "end-of-month inventory levels by store",
        "monthly revenue from customers with at least 10 orders by store",
    ]
    try:
        for intent in intents:
            # Wider candidate window: as the metric registry grew (direct
            # rolling/prior_period/period_to_date/derived demos), nearby
            # share/ratio metrics for the "food vs drink" intent get
            # ranked alongside more candidates. Bumping limit keeps the
            # ≥3 invariant without weakening it.
            result = plan_candidate_envelope(
                runtime,
                intent=intent,
                partial_query={"policy_context": {"now": "2026-05-04"}},
                limit=20,
            )
            candidates = list(result.get("candidates", []) or [])
            assert len(candidates) >= 3, intent
            assert any(
                candidate.get("validation", {}).get("ok") is True for candidate in candidates
            ), intent
    finally:
        runtime.close()


def test_build_options_ranks_recommended_dimensions_and_stock_aggregations(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        orders_options = build_options_payload(
            runtime,
            partial_query={
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
            },
            focus_terms="orders by store",
            focus_object_id="measure.jaffle.order_count",
            stage="post_measure",
            limit=5,
        )
        stock_options = build_options_payload(
            runtime,
            partial_query={"version": 1, "select": []},
            focus_object_id="measure.jaffle.inventory_on_hand_eop",
            step="aggregation",
            limit=10,
        )

        assert orders_options["recommended"]
        assert orders_options["stage"] == "post_measure"
        assert orders_options["builder_step"] == "group_by"
        assert orders_options["recommended"][0]["id"] == "dimension.jaffle_store_name"
        assert orders_options["recommended"][0]["object_type"] == "dimension"
        assert orders_options["recommended"][0]["query_patch"]["group_by"] == [
            "dimension.jaffle_store_name"
        ]

        stock_aggs = {
            row["id"]: row
            for row in [
                *stock_options["recommended"],
                *stock_options["available"],
                *stock_options["blocked"],
            ]
        }
        assert stock_aggs["last_value"]["available"] is True
        assert stock_aggs["first_value"]["available"] is True
        assert stock_aggs["sum"]["available"] is True
        assert stock_aggs["percentile"]["available"] is True
    finally:
        runtime.close()


def test_valid_values_supports_search_paging_and_selection_context(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = valid_values_payload(
            runtime,
            dimension_id="dimension.jaffle_item_product_type",
            search="drink",
            limit=1,
            offset=0,
        )
        assert payload["source"] == "value_domain"
        assert payload["selection_context"]["dimension_id"] == "dimension.jaffle_item_product_type"
        assert payload["total_count"] == 1
        assert payload["has_more"] is False
        assert payload["value_source_type"] == "declared_domain"
        assert payload["estimated_cost"] == "none"
        assert payload["values"][0]["value"] == "beverage"
    finally:
        runtime.close()


def test_plan_supports_guided_query_building(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        planned = plan_candidate_envelope(runtime, intent="new customer orders over time", limit=2)
        conversion = plan_candidate_envelope(
            runtime, intent="session to order conversion rate", limit=2
        )
        contextual = plan_candidate_envelope(
            runtime,
            intent="monthly order volume for customers that made more than 10 purchases in that month",
            limit=2,
        )
        revenue_qualified = plan_candidate_envelope(
            runtime,
            intent="monthly revenue from customers with at least 10 orders by store",
            limit=2,
        )
        daily_qualified = plan_candidate_envelope(
            runtime,
            intent="daily order volume from customers with at least 10 orders in that month",
            limit=2,
        )

        assert planned["candidates"]
        candidate_query = planned["candidates"][0]["candidate_ir"]
        assert candidate_query["time"]["grain"] == "month"
        assert planned["candidates"][0]["validation"]["ok"] is True
        assert planned["candidates"][0]["resolved"]
        assert "assumptions" in planned["candidates"][0]
        assert contextual["candidates"]
        contextual_query = contextual["candidates"][0]["candidate_ir"]
        assert contextual["interpreted_intent"]["pattern"] == "qualified_metric_rollup"
        assert contextual_query["select"][0]["expression"]["kind"] == "scoped_aggregate"
        # The predicate carries the customer entity, op, and value — the
        # input ref may be a measure or a metric depending on which scored
        # higher in discovery for the predicate term.
        contextual_predicate = contextual_query["select"][0]["expression"]["predicates"][0]
        assert contextual_predicate["entity"] == "entity.jaffle_customer"
        assert contextual_predicate["op"] == ">"
        assert contextual_predicate["value"] == 10
        assert "measure" in contextual_predicate or "metric" in contextual_predicate
        assert any(
            row["id"] == "entity.jaffle_customer" for row in contextual["candidates"][0]["resolved"]
        )
        assert contextual["candidates"][0]["validation"]["ok"] is True
        revenue_query = revenue_qualified["candidates"][0]["candidate_ir"]
        revenue_expr = revenue_query["select"][0]["expression"]
        assert revenue_qualified["interpreted_intent"]["pattern"] == "qualified_metric_rollup"
        assert revenue_expr["measure"] == "measure.jaffle.revenue_usd"
        # Predicate input may be measure or metric; assert the cohort fields.
        revenue_predicate = revenue_expr["predicates"][0]
        assert revenue_predicate["entity"] == "entity.jaffle_customer"
        assert revenue_predicate["op"] == ">="
        assert revenue_predicate["value"] == 10
        assert "measure" in revenue_predicate or "metric" in revenue_predicate
        assert revenue_query["group_by"] == ["dimension.jaffle_store_name"]
        daily_expr = daily_qualified["candidates"][0]["candidate_ir"]["select"][0]["expression"]
        assert daily_expr["predicates"][0]["time_grain"] == "month"
        assert "time_alignment" not in daily_expr["predicates"][0]
        assert conversion["candidates"]
        assert conversion["candidates"][0]["validation"]["ok"] is True
    finally:
        runtime.close()


def test_plan_generic_multi_dimension_and_current_month_bounds(tmp_path: Path):
    package_dir = tmp_path / "generic_planning_demo"
    _write_generic_planning_package(package_dir)
    runtime = Runtime.from_path(str(package_dir))
    try:
        planned = plan_candidate_envelope(
            runtime,
            intent="sends per account by product and send type for current month",
            limit=1,
        )
        today = date.today()
        month_start = today.replace(day=1)
        next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)

        assert planned["candidates"]
        query = planned["candidates"][0]["candidate_ir"]
        assert planned["candidates"][0]["validation"]["ok"] is True
        assert query["select"][0]["expression"] == {"metric": "metric.engagement.sends_per_account"}
        assert query["group_by"] == ["dimension.demo_product", "dimension.demo_send_type"]
        assert query["time"] == {
            "temporal_role": "temporal_role.demo_event_time",
            "grain": "month",
            "start": month_start.isoformat(),
            "end": next_month.isoformat(),
        }
    finally:
        runtime.close()


def test_plan_generic_q4_bounds_and_paying_filter_not_group_by(tmp_path: Path):
    package_dir = tmp_path / "generic_planning_demo"
    _write_generic_planning_package(package_dir)
    runtime = Runtime.from_path(str(package_dir))
    try:
        planned = plan_candidate_envelope(
            runtime,
            intent="utilization rate by paying account and send type in Q4 2025",
            limit=1,
        )

        assert planned["candidates"]
        query = planned["candidates"][0]["candidate_ir"]
        assert planned["candidates"][0]["validation"]["ok"] is True
        assert query["select"][0]["expression"] == {"metric": "metric.engagement.utilization_rate"}
        assert query["group_by"] == ["dimension.demo_send_type"]
        assert query["where"] == [
            {"field": "dimension.demo_account_status", "op": "=", "value": "paying"}
        ]
        assert query["time"] == {
            "temporal_role": "temporal_role.demo_event_time",
            "grain": "quarter",
            "start": "2025-10-01",
            "end": "2026-01-01",
        }
    finally:
        runtime.close()


def test_plan_avoids_irrelevant_value_filters(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        top_stores = plan_candidate_envelope(runtime, intent="top stores by revenue", limit=2)
        new_customer_trend = plan_candidate_envelope(
            runtime, intent="new customer orders over time", limit=2
        )

        assert top_stores["candidates"]
        assert "where" not in top_stores["candidates"][0]["candidate_ir"]
        assert new_customer_trend["candidates"]
        assert "where" not in new_customer_trend["candidates"][0]["candidate_ir"]
    finally:
        runtime.close()


def test_plan_does_not_invent_generic_dimension_value_filters(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        planned = plan_candidate_envelope(runtime, intent="top stores by revenue", limit=2)
        assert planned["candidates"]
        candidate_query = planned["candidates"][0]["candidate_ir"]
        assert "where" not in candidate_query or candidate_query["where"] == []
        assert candidate_query["group_by"] == ["dimension.jaffle_store_name"]
    finally:
        runtime.close()


def test_plan_supports_comparison_candidates_and_legal_alternatives(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        food_vs_drink = plan_candidate_envelope(
            runtime, intent="food vs drink revenue share by month", limit=3
        )
        ordered_vs_delivered = plan_candidate_envelope(
            runtime, intent="ordered vs delivered revenue by month", limit=3
        )

        assert food_vs_drink["candidates"] or food_vs_drink["blocked"]
        if food_vs_drink["candidates"]:
            select = food_vs_drink["candidates"][0]["candidate_ir"]["select"]
            assert len(select) == 2

        assert ordered_vs_delivered["candidates"] or ordered_vs_delivered["blocked"]
        if ordered_vs_delivered["blocked"]:
            assert ordered_vs_delivered["blocked"][0]["closest_legal_alternatives"]
            assert ordered_vs_delivered["comparison_bundle"]
            resolved_ids = {
                row["id"]
                for row in ordered_vs_delivered["blocked"][0]["resolved"]
                if isinstance(row, dict) and row.get("id")
            }
            # Wrappers metric.sales.delivered_revenue / metric.sales.revenue_usd
            # were dropped; comparison machinery now anchors to the underlying
            # measures with the same comparison_family.
            assert "measure.jaffle.delivered_revenue_usd" in resolved_ids
            assert "measure.jaffle.revenue_usd" in resolved_ids
            bundle_roles = {
                row["query_patch"]["time"]["temporal_role"]
                for row in ordered_vs_delivered["comparison_bundle"]
                if row.get("query_patch", {}).get("time")
            }
            assert bundle_roles == {
                "temporal_role.jaffle_lifecycle_delivered_at",
                "temporal_role.jaffle_order_time",
            }
            assert all(
                row["query_patch"]["time"]["grain"] == "month"
                for row in ordered_vs_delivered["comparison_bundle"]
            )
    finally:
        runtime.close()


def test_build_options_prefers_human_readable_business_dimensions(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        store_groups = build_options_payload(
            runtime,
            partial_query={
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "Revenue (USD)"}
                ],
            },
            step="group_by",
            focus_terms="top stores by revenue",
            limit=5,
        )
        aov_groups = build_options_payload(
            runtime,
            partial_query={
                "version": 1,
                "select": [
                    {"expression": {"metric": "metric.sales.aov_usd"}, "as": "Average order value"}
                ],
            },
            step="group_by",
            focus_terms="average order value by customer type",
            limit=5,
        )
        history_groups = build_options_payload(
            runtime,
            partial_query={
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "Revenue (USD)"}
                ],
            },
            step="group_by",
            focus_terms="historical revenue by customer segment",
            limit=5,
        )

        assert store_groups["recommended"][0]["id"] == "dimension.jaffle_store_name"
        assert any(
            row["id"] == "dimension.jaffle_customer_type" for row in aov_groups["recommended"]
        )
        assert history_groups["recommended"][0]["id"] == "dimension.jaffle_customer_history_segment"
        assert store_groups["query_patches"][0]["group_by"] == ["dimension.jaffle_store_name"]
    finally:
        runtime.close()


def test_build_options_time_prefers_metric_default_temporal_role(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = build_options_payload(
            runtime,
            partial_query={
                "version": 1,
                "select": [
                    {
                        "expression": {"measure": "measure.jaffle.delivered_revenue_usd"},
                        "as": "Delivered revenue",
                    }
                ],
            },
            step="time",
            focus_terms="delivered revenue by delivered month",
            focus_object_id="measure.jaffle.delivered_revenue_usd",
            limit=5,
        )

        assert payload["recommended"]
        assert payload["recommended"][0]["id"] == "temporal_role.jaffle_lifecycle_delivered_at"
        assert payload["recommended"][0]["query_patch"]["time"] == {
            "temporal_role": "temporal_role.jaffle_lifecycle_delivered_at",
            "grain": "month",
        }
        assert all(
            row["id"].startswith("temporal_role.jaffle_lifecycle_")
            for row in payload["recommended"] + payload["available"]
        )
    finally:
        runtime.close()


def test_history_backed_queries_emit_null_preserving_caveats(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        query = {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "Revenue (USD)"}
            ],
            "group_by": ["dimension.jaffle_customer_history_segment"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        }

        inspected = inspect_payload(
            runtime,
            object_id="dimension.jaffle_customer_history_segment",
            partial_query={"version": 1, "select": query["select"]},
        )
        validated = runtime.validate(query)
        compiled = runtime.compile(query)
        queried = runtime.query(query)

        assert inspected["card"]["null_bucket_meaning"]
        assert any(row["code"] == "NULL_PRESERVING_HISTORY" for row in validated["warnings"])
        assert any(row["code"] == "NULL_PRESERVING_HISTORY" for row in compiled["warnings"])
        assert any(row["code"] == "NULL_PRESERVING_HISTORY" for row in queried["warnings"])
    finally:
        runtime.close()


def test_query_request_limits_clip_rows_for_duckdb(runtime_factory):
    """The `limits.max_rows` field on the request envelope must clip
    DuckDB results during cursor fetch. The `statement_timeout_ms` field is
    accepted (no-op on DuckDB) without raising.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        query = {
            "version": 1,
            "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
            "group_by": ["dimension.jaffle_store_name"],
        }

        # No limits: baseline row count.
        baseline = runtime.query(query)
        baseline_rows = list(baseline["rows"])
        assert len(baseline_rows) > 1
        assert baseline["truncated"] is False

        # max_rows clips the result.
        clipped = runtime.query({**query, "limits": {"max_rows": 1}})
        assert len(clipped["rows"]) == 1
        assert clipped["truncated"] is True

        # statement_timeout_ms is enforced by DuckDB's interrupt watchdog.
        timed = runtime.query({**query, "limits": {"statement_timeout_ms": 60_000}})
        assert len(timed["rows"]) == len(baseline_rows)
        timeout_warnings = [
            w
            for w in timed.get("warnings", []) or []
            if dict(w).get("code") == "STATEMENT_TIMEOUT_NOT_HONORED"
        ]
        assert timeout_warnings == []

        # When limits don't include statement_timeout_ms, no warning fires.
        only_rows = runtime.query({**query, "limits": {"max_rows": 1}})
        assert not [
            w
            for w in only_rows.get("warnings", []) or []
            if dict(w).get("code") == "STATEMENT_TIMEOUT_NOT_HONORED"
        ]

        # Invalid limits values are silently ignored.
        ignored = runtime.query({**query, "limits": {"max_rows": "not-an-int"}})
        assert len(ignored["rows"]) == len(baseline_rows)

        # Limits in the wrong shape don't blow up.
        wrong_shape = runtime.query({**query, "limits": [1, 2, 3]})
        assert len(wrong_shape["rows"]) == len(baseline_rows)
    finally:
        runtime.close()


def test_legacy_adapter_without_limits_kwarg_still_works(runtime_factory, monkeypatch):
    """A custom warehouse adapter with the legacy ``query(sql)`` signature
    must keep working even though the runtime now passes per-request
    limits. Third-party hosting integrations rely on this — we cannot
    break their adapters by silently growing the call signature.
    """
    runtime = runtime_factory("jaffle_shop")
    try:

        class _LegacyAdapter:
            engine = "duckdb"
            supports_statement_timeout = False

            def __init__(self, real):
                self._real = real
                self.calls: list[str] = []

            def query(self, sql):  # noqa: D401 — legacy signature, no `limits=`
                self.calls.append(sql)
                return list(self._real.query(sql))

            def close(self) -> None:
                return None

        real_adapter = runtime._get_adapter()
        legacy = _LegacyAdapter(real_adapter)
        monkeypatch.setattr(runtime, "_get_adapter", lambda: legacy)

        result = runtime.query(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
                ],
                "limit": 1,
                "limits": {"max_rows": 1, "statement_timeout_ms": 5_000},
            }
        )
        assert result["rows"], result
        assert legacy.calls, "legacy adapter should have been queried"
    finally:
        runtime.close()


def test_snowflake_adapter_query_signatures_accept_limits():
    """Both Snowflake adapter classes must accept the `limits` kwarg on
    their `query` method so per-tenant resource limits can pass through.
    Skipped if snowflake-connector-python is not installed in the test
    environment (we only need the signature, not a live connection).
    """
    import importlib.util as _import_util
    import inspect

    try:
        from semantic_rails.db import SnowflakeCliAdapter
    except ImportError:
        import pytest as _pytest_skip

        _pytest_skip.skip("SnowflakeCliAdapter not importable")
        return

    sig = inspect.signature(SnowflakeCliAdapter.query)
    assert "limits" in sig.parameters
    assert sig.parameters["limits"].default is None

    try:
        has_connector = _import_util.find_spec("snowflake.connector") is not None
    except (ModuleNotFoundError, ValueError):
        has_connector = False
    if not has_connector:
        return  # native adapter requires the connector — skip its check

    from semantic_rails.db import SnowflakeNativeAdapter

    sig_native = inspect.signature(SnowflakeNativeAdapter.query)
    assert "limits" in sig_native.parameters
    assert sig_native.parameters["limits"].default is None


def test_runtime_reload_rebuilds_state_after_package_edit(tmp_path):
    """Runtime.reload() must re-read the package YAML, rebuild the registry,
    while preserving the injected cache backend so config edits take effect
    without restarting the process.
    """
    from tests.semantic_rails.conftest import copy_package_config

    package_root = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_path(str(package_root))
    try:
        original_fingerprint = runtime._package_fingerprint
        original_object_count = len(list(runtime.registry.list_objects()))

        # Force a compile so the cache has at least one entry.
        runtime.validate(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
                "limit": 5,
            }
        )
        compile_cache = runtime._compile_cache
        cached_before = len(runtime._compile_cache._items)
        assert cached_before >= 1
        runtime._manifest = {"catalog_json": {"summary|compact": "{}"}}

        # Append a comment to a metrics YAML so the package fingerprint changes.
        metrics_file = package_root / "metrics" / "core" / "core_metrics.yml"
        metrics_file.write_text(
            metrics_file.read_text() + "\n# reload-test-marker\n",
            encoding="utf-8",
        )

        result = runtime.reload()

        assert result["package_id"] == runtime.package_id
        assert result["changed"] is True
        assert result["previous_fingerprint"] == original_fingerprint
        assert result["package_fingerprint"] != original_fingerprint
        # Backend identity is preserved; package-fingerprinted keys prevent
        # the old entry from being reused after reload.
        assert runtime._compile_cache is compile_cache
        assert len(runtime._compile_cache._items) == cached_before
        assert runtime._manifest is None
        # Registry rebuilt and still functional (same object count after
        # a comment-only edit).
        assert len(list(runtime.registry.list_objects())) == original_object_count
        # Catalog cache cleared (rebuilt on next access).
        assert runtime._catalog_cache is None

        runtime.validate(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
                "limit": 5,
            }
        )
        assert len(runtime._compile_cache._items) == cached_before + 1

        # Reload is idempotent — calling again with no further edits
        # reports `changed=False`.
        second = runtime.reload()
        assert second["changed"] is False
        assert second["package_fingerprint"] == result["package_fingerprint"]
    finally:
        runtime.close()


@pytest.mark.parametrize("operation", ["compile", "execute", "segment"])
def test_reload_waits_for_atomic_governed_request(
    runtime_factory, monkeypatch, operation: str
) -> None:
    """Reload must not swap config/registry/adapter midway through a request."""

    import semantic_rails.runtime as runtime_module

    runtime = runtime_factory("jaffle_shop")
    entered = threading.Event()
    release = threading.Event()

    if operation == "compile":
        original = runtime_module.compile_query

        def blocking(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=5)
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime_module, "compile_query", blocking)

        def invoke():
            return runtime.compile(
                {
                    "version": 1,
                    "select": [
                        {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                    ],
                    "limit": 1,
                }
            )
    elif operation == "execute":
        original = runtime_module._adapter_query

        def blocking(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=5)
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime_module, "_adapter_query", blocking)

        def invoke():
            return runtime.query(
                {
                    "version": 1,
                    "select": [
                        {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                    ],
                    "limit": 1,
                }
            )
    else:
        original = runtime_module.normalize_segment

        def blocking(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=5)
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime_module, "normalize_segment", blocking)

        def invoke():
            return runtime.segment_preview("segment.jaffle.high_value_customers", limit=1)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            request_future = pool.submit(invoke)
            assert entered.wait(timeout=3)
            reload_future = pool.submit(runtime.reload)
            time.sleep(0.05)
            assert not reload_future.done(), "reload crossed an in-flight request boundary"
            release.set()
            assert request_future.result(timeout=10).get("ok", True) is True
            assert reload_future.result(timeout=10)["changed"] is False
    finally:
        release.set()
        runtime.close()


@pytest.mark.parametrize("operation", ["plan", "inspect"])
def test_reload_waits_for_complete_shared_application_operation(
    runtime_factory, monkeypatch, operation: str
) -> None:
    """Planner and metadata work must not mix runtime generations.

    These operations read config before they eventually call a governed
    Runtime method (and some metadata paths never call one). Holding the
    shared request scope at the application function keeps reload from
    replacing config/registry in the middle of either transport-independent
    operation.
    """

    runtime = runtime_factory("jaffle_shop")
    entered = threading.Event()
    release = threading.Event()

    if operation == "plan":
        import semantic_rails.planner.plan as plan_module

        original = plan_module.compose

        def blocking(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=5)
            return original(*args, **kwargs)

        monkeypatch.setattr(plan_module, "compose", blocking)

        def invoke():
            return plan_module.plan_payload(runtime, intent="orders by store")

    else:
        import semantic_rails.metadata as metadata_module

        original = metadata_module._object_card

        def blocking(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=5)
            return original(*args, **kwargs)

        monkeypatch.setattr(metadata_module, "_object_card", blocking)

        def invoke():
            return metadata_module.inspect_payload(
                runtime,
                object_id="measure.jaffle.order_count",
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            request_future = pool.submit(invoke)
            assert entered.wait(timeout=3)
            reload_future = pool.submit(runtime.reload)
            time.sleep(0.05)
            assert not reload_future.done(), "reload crossed a shared application boundary"
            release.set()
            assert request_future.result(timeout=10)
            assert reload_future.result(timeout=10)["changed"] is False
    finally:
        release.set()
        runtime.close()


def test_slow_query_does_not_block_catalog_health_or_plan(runtime_factory, monkeypatch) -> None:
    """Generation readers overlap; only reload/runtime mutation is exclusive."""

    import semantic_rails.runtime as runtime_module
    from semantic_rails.catalog_service import resolve_catalog
    from semantic_rails.http_core import SemanticHTTPService
    from semantic_rails.planner.plan import plan_payload

    runtime = runtime_factory("jaffle_shop")
    entered = threading.Event()
    release = threading.Event()

    def slow_adapter_query(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return [{"orders": 1}]

    monkeypatch.setattr(runtime_module, "_adapter_query", slow_adapter_query)
    query = {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
        "limit": 1,
    }

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            query_future = pool.submit(runtime.query, query)
            assert entered.wait(timeout=3)

            catalog_future = pool.submit(resolve_catalog, runtime)
            plan_future = pool.submit(plan_payload, runtime, intent="orders by store")
            health_future = pool.submit(SemanticHTTPService(runtime).health_payload)

            assert catalog_future.result(timeout=3)["meta"]["package"]["package_id"]
            assert plan_future.result(timeout=3)["status"] in {
                "ok",
                "low_confidence",
                "unrealizable",
            }
            assert health_future.result(timeout=3)["ok"] is True
            assert not query_future.done(), "the slow warehouse query unexpectedly finished"

            release.set()
            assert query_future.result(timeout=10)["ok"] is True
    finally:
        release.set()
        runtime.close()


def test_query_execution_error_redacts_sql_by_default(runtime_factory, monkeypatch):
    """QUERY_EXECUTION_ERROR details must NOT contain rendered SQL by default.

    Rendered SQL leaks column names, join predicates, and literal filter
    values into downstream logging sinks — a problem for MNPI-tagged
    tables in a hosted deployment. The default is a redacted summary
    (sha256 + outline + char count). Raw SQL is gated behind three
    conditions: ``debug: true`` in the request, the operator-controlled
    env var ``SEMANTIC_RAILS_ALLOW_DEBUG_SQL=1``, AND the ``debug`` role
    in the resolved request context. The env var exists because the
    bundled ``HeaderPolicyContextResolver`` lets a client self-assert
    roles via headers/body — operators must opt in before role-based
    SQL exposure is honoured.
    """
    from semantic_rails.errors import SemanticLayerError

    runtime = runtime_factory("jaffle_shop")
    try:

        class _BoomAdapter:
            engine = "duckdb"

            def query(self, sql: str):
                raise RuntimeError("duckdb adapter exploded for test")

            def close(self) -> None:
                return None

        monkeypatch.setattr(runtime, "_get_adapter", lambda: _BoomAdapter())

        base_query = {
            "version": 1,
            "select": [
                {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"},
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "limit": 5,
        }

        # Default mode: SQL must be redacted.
        import pytest as _pytest

        with _pytest.raises(SemanticLayerError) as exc_info:
            runtime.query(dict(base_query))
        details = dict(exc_info.value.details)
        assert exc_info.value.code == "QUERY_EXECUTION_ERROR"
        assert "sql" not in details, "raw SQL leaked in default error details"
        assert details.get("sql_redacted") is True
        assert details.get("sql_sha256")
        assert details.get("sql_chars", 0) > 0
        assert "select" in details.get("sql_outline", [])

        # debug=true alone (no debug role) must NOT expose raw SQL.
        with _pytest.raises(SemanticLayerError) as exc_info:
            runtime.query({**base_query, "debug": True})
        details = dict(exc_info.value.details)
        assert "sql" not in details
        assert details.get("sql_redacted") is True

        # debug=true plus the `debug` role, but without the operator env
        # opt-in, must STILL redact. The OSS default treats roles as
        # client-assertable, so role membership alone is not authority.
        with _pytest.raises(SemanticLayerError) as exc_info:
            runtime.query(
                {
                    **base_query,
                    "debug": True,
                    "policy_context": {"roles": ["debug"]},
                }
            )
        details = dict(exc_info.value.details)
        assert "sql" not in details, "raw SQL leaked when only role was asserted"
        assert details.get("sql_redacted") is True

        # Operator env opt-in + debug=true + debug role → SQL is included.
        monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_DEBUG_SQL", "1")
        with _pytest.raises(SemanticLayerError) as exc_info:
            runtime.query(
                {
                    **base_query,
                    "debug": True,
                    "policy_context": {"roles": ["debug"]},
                }
            )
        details = dict(exc_info.value.details)
        assert details.get("sql_debug_authorized") is True
        assert isinstance(details.get("sql"), str) and details["sql"].strip().lower().startswith(
            ("select", "with")
        )

        # Env opt-in alone (no debug role) must still redact.
        with _pytest.raises(SemanticLayerError) as exc_info:
            runtime.query({**base_query, "debug": True})
        details = dict(exc_info.value.details)
        assert "sql" not in details
        assert details.get("sql_redacted") is True
    finally:
        runtime.close()


def test_build_options_empty_query_ranks_by_review_priority(runtime_factory):
    """Empty-query, empty-terms used to give every candidate the same
    flat rank (the per-kind stage bonus) with rationale
    "default ordering" — pure noise. With no scoring context, rank by
    `review_priority` and surface a topic-based rationale so the rank
    field actually differentiates candidates."""
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = build_options_payload(runtime, partial_query={})
        recommended = list(payload.get("recommended", []) or [])
        assert recommended, "expected recommended candidates"
        ranks = sorted({float(row.get("rank", 0.0) or 0.0) for row in recommended})
        assert len(ranks) >= 2, (
            f"expected rank to differentiate candidates on empty query; got ranks={ranks}"
        )
        for row in recommended:
            why = str(row.get("why_recommended", "") or "")
            assert why and why != "default ordering", (
                f"expected a meaningful why_recommended on empty query; row={row.get('id')} got {why!r}"
            )
    finally:
        runtime.close()


def test_build_options_ships_next_legal_steps(runtime_factory):
    """Blind-agent UX: build-options must enumerate the remaining legal
    builder steps, not just the next single step. ``review`` is always
    the tail."""
    runtime = runtime_factory("jaffle_shop")
    try:
        empty = build_options_payload(runtime, partial_query={})
        assert empty["next_legal_steps"] == [
            "measure",
            "group_by",
            "filter_dimension",
            "time",
            "review",
        ]
        assert empty["builder_step"] == "measure"

        with_select = build_options_payload(
            runtime,
            partial_query={
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "Revenue (USD)"}
                ],
            },
        )
        # measure satisfied — first remaining is group_by, review still tails.
        assert with_select["next_legal_steps"][0] == "group_by"
        assert with_select["next_legal_steps"][-1] == "review"
        assert "measure" not in with_select["next_legal_steps"]

        fully_specified = build_options_payload(
            runtime,
            partial_query={
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.revenue_usd"}, "as": "Revenue (USD)"}
                ],
                "group_by": ["dimension.jaffle_store_name"],
                "where": [{"field": "dimension.jaffle_store_name", "op": "=", "value": "anywhere"}],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            },
        )
        # Everything satisfied — review only.
        assert fully_specified["next_legal_steps"] == ["review"]
    finally:
        runtime.close()
