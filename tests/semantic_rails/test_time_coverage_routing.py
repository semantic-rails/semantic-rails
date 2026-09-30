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


def _query(*expressions, **time):
    return {
        "select": [
            {"expression": e, "as": f"v{i}"} for i, e in enumerate(expressions or (REVENUE,))
        ],
        "time": {"temporal_role": ROLE, "grain": "month", **time},
    }


@pytest.mark.parametrize(
    "variant", ["utc_authored", "utc_implicit", "ny_implicit", "date_authored", "tz_implicit"]
)
def test_filled_monthly_answer_is_identical_with_and_without_rollups(tmp_path, variant):
    package = _write_variant(tmp_path, variant)
    config = load_package_config(str(package))
    query = _query(start="2023-10-01", end="2024-09-01", fill=True)
    answers = []
    for current in (config, replace(config, aggregate_relations=[])):
        runtime = Runtime.from_config(current, source_path=str(package))
        try:
            result = runtime.query(query)
            assert "FROM orders_monthly" not in result["rendered_sql"]
            answers.append(sorted(result["rows"], key=lambda r: str(r[f"{ROLE}__month"])))
        finally:
            runtime.close()
    assert answers[0] == answers[1]
    october = next(r for r in answers[0] if str(r[f"{ROLE}__month"]).startswith("2023-10-01"))
    assert october["v0"] is None


@pytest.mark.parametrize(
    ("expressions", "time", "routes"),
    [
        ((REVENUE,), {}, True),
        ((REVENUE,), {"start": "2024-02-01", "end": "2024-03-01"}, True),
        ((REVENUE,), {"fill": True}, False),
        (
            ({"kind": "rolling", "input": REVENUE, "window": {"unit": "month", "value": 3}},),
            {},
            False,
        ),
        (
            (REVENUE, {"measure": "measure.shop.refund_count"}),
            {"start": "2024-02-01", "end": "2024-03-01"},
            False,
        ),
        ((REVENUE, {"measure": "measure.shop.refund_count"}), {}, True),
    ],
    ids=["plain", "plain-bounded", "fill", "dense", "combined-bounded", "combined-unbounded"],
)
def test_only_coverage_dependent_plans_bypass_rollups(tmp_path, expressions, time, routes):
    config = load_package_config(str(_write_variant(tmp_path, "utc_authored")))
    compiled = compile_query(config, Registry(config), _query(*expressions, **time))
    report = compiled["performance_plan"].aggregate_routing
    assert bool(report["selected"]) is routes
    if not routes:
        assert "FROM orders_monthly" not in compiled["sql"]
        assert any(r["reason"] == "base_time_coverage_required" for r in report["candidates"])


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
