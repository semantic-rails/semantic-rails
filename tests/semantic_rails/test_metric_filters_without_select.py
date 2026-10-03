"""Distinct groups retain their filters, or refuse before returning SQL."""

from __future__ import annotations

import duckdb
import pytest

from semantic_rails.compiler import compile_query, plan_query
from semantic_rails.compiler_parts.sql_lowering import _distinct_value_select
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.segments import build_segment_query, normalize_segment
from tests.semantic_rails.conftest import copy_package_config

STORE = "dimension.jaffle_store_name"
REVENUE = {"measure": "measure.jaffle.revenue_usd"}


def _customer_filter(threshold=100, scope_mode="entity_only"):
    return {
        "expression": {
            "kind": "metric_predicate",
            "entity": "entity.jaffle_customer",
            "scope_mode": scope_mode,
            "input": REVENUE,
            "op": ">=",
            "value": threshold,
        },
        "op": "=",
        "value": True,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"select": []},
        {"select": [{"expression": {"kind": "literal", "value": 1}, "as": "one"}]},
        {"where": [{"field": STORE, "op": "=", "value": "Brooklyn"}]},
        {"order_by": [{"field": STORE, "direction": "DESC"}], "limit": 1},
    ],
    ids=["omitted-select", "empty-select", "literal-select", "where", "order-limit"],
)
@pytest.mark.parametrize("scope_mode", ["entity_only", "contextual"])
def test_metric_predicate_without_leaf_refuses(package_config_factory, extra, scope_mode):
    config, _ = package_config_factory("jaffle_shop")
    query = {
        "group_by": [STORE],
        "metric_filters": [_customer_filter(scope_mode=scope_mode)],
        **extra,
    }
    with pytest.raises(SemanticLayerError) as exc:
        compile_query(config, Registry(config), query)
    assert exc.value.code == "PREDICATE_NOT_SUPPORTED"
    assert exc.value.details["path"] == "metric_filters"
    assert "add a select" in str(exc.value)
    assert "remove metric_filters" in str(exc.value)


def test_direct_distinct_lowering_cannot_bypass_predicate_guard(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    plan = plan_query(
        config, Registry(config), {"group_by": [STORE], "metric_filters": [_customer_filter()]}
    )
    with pytest.raises(SemanticLayerError) as exc:
        _distinct_value_select(plan, config)
    assert exc.value.code == "PREDICATE_NOT_SUPPORTED"


@pytest.fixture()
def warehouse(tmp_path):
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    config = load_package_config(str(package))
    with duckdb.connect(str(package / "jaffle_shop.duckdb"), read_only=True) as conn:
        yield config, conn


@pytest.mark.parametrize("threshold", [0, 100000, 10**12])
def test_aggregate_filter_without_select_matches_reference_sql(warehouse, threshold):
    config, conn = warehouse
    query = {
        "group_by": [STORE],
        "metric_filters": [{"expression": REVENUE, "op": ">=", "value": threshold}],
    }
    expected = conn.execute(
        "SELECT s.store_name FROM jaffle_order o "
        "LEFT JOIN jaffle_store s ON o.store_id = s.store_id "
        "GROUP BY s.store_name HAVING SUM(o.order_total_cents / 100.0) >= ?",
        [threshold],
    ).fetchall()
    actual = conn.execute(compile_query(config, Registry(config), query)["sql"]).fetchall()
    assert sorted(actual) == sorted(expected)


@pytest.mark.parametrize("threshold", [100, 10000, 10**12])
@pytest.mark.parametrize("selected", [True, False], ids=["select", "filter-leaf-only"])
def test_metric_predicate_with_leaf_matches_reference_sql(warehouse, threshold, selected):
    config, conn = warehouse
    query = {
        "select": [{"expression": REVENUE, "as": "revenue"}],
        "group_by": [STORE],
        "metric_filters": [_customer_filter(threshold)],
    }
    if not selected:
        query.pop("select")
        query["metric_filters"].append({"expression": REVENUE, "op": ">=", "value": 0})
    expected = conn.execute(
        "WITH qualifying AS (SELECT customer_id FROM jaffle_order "
        "GROUP BY customer_id HAVING SUM(order_total_cents / 100.0) >= ?) "
        "SELECT s.store_name, SUM(o.order_total_cents / 100.0) FROM jaffle_order o "
        "JOIN qualifying q ON o.customer_id = q.customer_id "
        "LEFT JOIN jaffle_store s ON o.store_id = s.store_id GROUP BY s.store_name",
        [threshold],
    ).fetchall()
    actual = conn.execute(compile_query(config, Registry(config), query)["sql"]).fetchall()
    if not selected:
        assert sorted(actual) == sorted((row[0],) for row in expected if row[1] >= 0)
        return
    actual, expected = sorted(actual), sorted(expected)
    assert [row[0] for row in actual] == [row[0] for row in expected]
    assert [row[1] for row in actual] == pytest.approx([row[1] for row in expected])


@pytest.mark.parametrize(
    "extra, reference",
    [
        (
            {"where": [{"field": STORE, "op": "=", "value": "Brooklyn"}]},
            "SELECT DISTINCT store_name FROM jaffle_store WHERE store_name = 'Brooklyn'",
        ),
        (
            {"order_by": [{"field": STORE, "direction": "DESC"}], "limit": 2},
            "SELECT DISTINCT store_name FROM jaffle_store ORDER BY store_name DESC LIMIT 2",
        ),
    ],
    ids=["where", "order-limit"],
)
def test_distinct_sibling_shapes_match_reference_sql(warehouse, extra, reference):
    config, conn = warehouse
    query = {"group_by": [STORE], **extra}
    actual = conn.execute(compile_query(config, Registry(config), query)["sql"]).fetchall()
    assert actual == conn.execute(reference).fetchall()


@pytest.mark.parametrize("key", ["filters", "segments"])
def test_unsupported_sibling_keys_refuse(package_config_factory, key):
    config, _ = package_config_factory("jaffle_shop")
    with pytest.raises(SemanticLayerError) as exc:
        compile_query(config, Registry(config), {"group_by": [STORE], key: []})
    assert exc.value.code == "INVALID_QUERY"


def test_authored_segment_keeps_membership_filter(warehouse):
    config, conn = warehouse
    segment = normalize_segment(config, "segment.jaffle.high_value_customers")
    query = build_segment_query(segment, include_preview_dimensions=False)
    actual = conn.execute(compile_query(config, Registry(config), query)["sql"]).fetchall()
    expected = conn.execute(
        "SELECT customer_id, 1 FROM jaffle_customer WHERE lifetime_spend_cents / 100.0 >= 100"
    ).fetchall()
    assert sorted(actual) == sorted(expected)
    query.pop("select")
    with pytest.raises(SemanticLayerError) as exc:
        compile_query(config, Registry(config), query)
    assert exc.value.code == "PREDICATE_NOT_SUPPORTED"
