"""Guard the naming and subject-reader boundaries used by planner drafts."""

import pytest

from semantic_rails.naming import last_token, semantic_token
from semantic_rails.planner.coverage import _projected_subject_ids as coverage_subjects
from semantic_rails.planner.plan_trace import _projected_subject_ids as trace_subjects


@pytest.mark.parametrize(
    ("value", "tail", "token"),
    [
        ("measure.jaffle.order_count", "order_count", "order_count"),
        ("entity.jaffle_customer", "jaffle_customer", "customer"),
        ("entity.sales_customer", "sales_customer", "customer"),
        ("metric.jaffle_revenue", "jaffle_revenue", "revenue"),
        ("metric.metric_recipe_measure_Été", "metric_recipe_measure_Été", "été"),
        ("measure.sales.Order--ID", "Order--ID", "order_id"),
        ("metric.東京", "東京", "東京"),
        ("metric.", "", "value"),
        ("___", "___", "value"),
    ],
)
def test_semantic_sql_names_preserve_prefixes_unicode_and_fallbacks(value, tail, token):
    assert last_token(value) == tail
    assert semantic_token(value) == token
    assert semantic_token(value, fallback="item") == ("item" if token == "value" else token)


@pytest.mark.parametrize(
    ("select", "coverage", "trace"),
    [
        ([], [], []),
        ([None, {}, {"expression": "measure.a"}], [], []),
        (
            [{"expression": {"metric": "metric.a", "measure": "measure.b"}}],
            ["metric.a"],
            ["metric.a"],
        ),
        ([{"expression": {"measure": "measure.a"}}] * 2, ["measure.a"], ["measure.a"]),
        ([{"expression": {"input": {"measure": "measure.a"}}}], [], []),
        ([{"expression": {"metric": "", "measure": "measure.a"}}], ["measure.a"], ["measure.a"]),
        ([{"expression": {"metric": 7, "measure": "measure.a"}}], [], ["7"]),
        ([{"expression": {"measure": True}}], [], ["True"]),
    ],
)
def test_projected_subject_readers_preserve_their_distinct_type_handling(select, coverage, trace):
    query = {"select": select}
    assert coverage_subjects(query) == coverage
    assert trace_subjects(query) == trace
