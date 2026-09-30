"""Coverage-dependent answers do not change when rollups are available."""

from dataclasses import replace

import pytest

from semantic_rails.compiler import compile_query, lower_to_sql
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from tests.integration.correctness.conftest import _write_variant

ROLE = "temporal_role.shop_order_ordered_at"
REVENUE = {"measure": "measure.shop.revenue"}
REFUNDS = {"measure": "measure.shop.refund_count"}
SIGNUPS = {"measure": "measure.shop.signup_count"}


def _query(*expressions, **time):
    return {
        "select": [
            {"expression": e, "as": f"v{i}"} for i, e in enumerate(expressions or (REVENUE,))
        ],
        "time": {"temporal_role": ROLE, "grain": "month", **time},
    }


def _answers(package, query):
    """The same query with and without the package's rollups, rows sorted by month."""
    config = load_package_config(str(package))
    answers = []
    for current in (config, replace(config, aggregate_relations=[])):
        runtime = Runtime.from_config(current, source_path=str(package))
        try:
            result = runtime.query(query)
            assert "FROM orders_monthly" not in result["rendered_sql"]
            answers.append(sorted(result["rows"], key=lambda r: str(r[f"{ROLE}__month"])))
        finally:
            runtime.close()
    return answers


@pytest.mark.parametrize(
    "variant", ["utc_authored", "utc_implicit", "ny_implicit", "date_authored", "tz_implicit"]
)
def test_filled_monthly_answer_is_identical_with_and_without_rollups(tmp_path, variant):
    routed, raw = _answers(
        _write_variant(tmp_path, variant), _query(start="2023-10-01", end="2024-09-01", fill=True)
    )
    assert routed == raw
    october = next(r for r in routed if str(r[f"{ROLE}__month"]).startswith("2023-10-01"))
    assert october["v0"] is None


def test_combined_unbounded_answer_is_identical_with_and_without_rollups(tmp_path):
    # A refund on a future-dated order: its month holds refunds only, after the orders' loaded
    # range. The order's NULL amount leaves the rollup's revenue exact for every month.
    package = _write_variant(tmp_path, "utc_authored")
    seed = package / "data" / "seed.sql"
    seed.write_text(
        seed.read_text(encoding="utf-8") + "\nINSERT INTO orders (order_id, ordered_at, amount) "
        "VALUES (999, TIMESTAMP '2098-01-15 00:00:00', NULL);"
        "\nINSERT INTO refunds (refund_id, order_id) VALUES (99, 999);",
        encoding="utf-8",
    )
    routed, raw = _answers(package, _query(REVENUE, REFUNDS))
    assert routed == raw
    future = next(r for r in routed if str(r[f"{ROLE}__month"]).startswith("2098-01-01"))
    assert (future["v0"], future["v1"]) == (None, 1)
    december = next(r for r in routed if str(r[f"{ROLE}__month"]).startswith("2023-12-01"))
    assert (december["v0"], december["v1"]) == (7, 0)  # a loaded month without refunds


ROUTING_CASES = {
    "plain": ((REVENUE,), {}, "duckdb", True),
    "plain-bounded": ((REVENUE,), {"start": "2024-02-01", "end": "2024-03-01"}, "duckdb", True),
    "fill": ((REVENUE,), {"fill": True}, "duckdb", False),
    "dense": (
        ({"kind": "rolling", "input": REVENUE, "window": {"unit": "month", "value": 3}},),
        {},
        "duckdb",
        False,
    ),
    "combined-bounded": (
        (REVENUE, REFUNDS),
        {"start": "2024-02-01", "end": "2024-03-01"},
        "duckdb",
        False,
    ),
    "combined-unbounded": ((REVENUE, REFUNDS), {}, "duckdb", False),
    "postgres-fill": ((REVENUE,), {"fill": True}, "postgres", False),
    # No coverage without execution evidence: routing and the in-window test stay as they were.
    "snowflake-fill": ((REVENUE,), {"fill": True}, "snowflake", True),
    "snowflake-combined": ((REVENUE, REFUNDS), {}, "snowflake", True),
}


@pytest.mark.parametrize(
    ("expressions", "time", "warehouse", "routes"),
    list(ROUTING_CASES.values()),
    ids=list(ROUTING_CASES),
)
def test_coverage_emitted_exactly_when_rollups_are_refused(
    tmp_path, expressions, time, warehouse, routes
):
    config = load_package_config(str(_write_variant(tmp_path, "utc_authored")))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    compiled = compile_query(config, Registry(config), _query(*expressions, **time))
    report = compiled["performance_plan"].aggregate_routing
    refused = any(r["reason"] == "base_time_coverage_required" for r in report["candidates"])
    assert bool(report["selected"]) is routes
    assert ("coverage_" in compiled["sql"]) is refused is (not routes)
    if not routes:
        assert "FROM orders_monthly" not in compiled["sql"]


def test_a_refused_route_keeps_the_leafs_own_strategy(tmp_path):
    base = load_package_config(str(_write_variant(tmp_path, "utc_authored")))
    strategies = {}
    for warehouse in ("duckdb", "snowflake"):
        config = replace(base, package=replace(base.package, warehouse=warehouse))
        compiled = compile_query(config, Registry(config), _query(SIGNUPS, REVENUE, fill=True))
        revenue = compiled["logical_plan"].measure_plans[1]
        strategies[warehouse] = (revenue.rewrite_strategy, revenue.aggregate_relation_id)
    assert strategies == {
        "duckdb": ("leaf_preaggregate_join", ""),
        "snowflake": ("aggregate_relation", "aggregate_relation.orders_monthly"),
    }


def test_injected_rollup_cannot_bypass_the_coverage_guard(tmp_path):
    config = load_package_config(str(_write_variant(tmp_path, "utc_authored")))
    compiled = compile_query(config, Registry(config), _query(fill=True))
    plan = compiled["logical_plan"]
    bypass = replace(
        plan,
        measure_plans=[
            replace(plan.measure_plans[0], aggregate_relation_id=config.aggregate_relations[0].id)
        ],
    )
    with pytest.raises(SemanticLayerError) as caught:
        lower_to_sql(bypass, config)
    assert caught.value.code == "EMPTY_GROUPS_UNSETTLED"
