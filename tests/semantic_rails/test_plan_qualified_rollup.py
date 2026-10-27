"""Qualified-metric rollups and inline year-over-year questions.

- A qualified rollup ("top 3 stores by order count with at least 4 distinct customers") keeps
  the qualifying entities with an entity filter whose conditions read the predicate measure,
  ``over`` the rows the question names; it ranks and limits only when the question asks for
  the top N. A qualification phrase that resolves to no measure is held with
  ``QUALIFICATION_NOT_REALIZABLE``.
- The predicate resolves to a measure the qualification names ("distinct customers" → a
  customer count), never an unrelated one (e.g. ``aov_usd``).
- "Revenue with YoY"-style questions ask whether to read the last two years as a series.
"""

from __future__ import annotations

import duckdb
import pytest

from semantic_rails.planner import plan_payload
from tests.plan_candidate_envelope import plan_candidate_envelope
from tests.semantic_rails.planner_fixtures import jaffle_runtime

runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)


def _reference(runtime, sql: str) -> list[tuple]:
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        return connection.execute(sql).fetchall()


@pytest.mark.parametrize(
    ("target", "alias", "aggregate"),
    [
        ("revenue", "revenue_usd", "SUM(o.order_total_cents / 100.0)"),
        ("order count", "order_count", "COUNT(DISTINCT o.order_id)"),
    ],
)
def test_a_top_n_qualified_rollup_ranks_the_qualifying_entities(
    runtime, target: str, alias: str, aggregate: str
) -> None:
    """No window: every order of each store qualifies it, the one reading, so plan answers."""
    plan = plan_payload(
        runtime, intent=f"top 3 stores by {target} with at least 4 distinct customers"
    )
    assert plan["status"] == "ok", plan.get("why")
    assert plan["best"]["pattern"] == "qualified_metric_rollup"
    query = plan["best"]["query"]
    assert query["where"] == [
        {
            "entity": "entity.jaffle_store",
            "having": [
                {"field": "measure.jaffle.ordering_customer_count", "op": "gte", "value": 4}
            ],
            "over": "all_time",
        }
    ]
    assert query["order_by"] == [{"field": alias, "direction": "desc"}]
    assert query["limit"] == 3
    rows = runtime.query(query)["rows"]
    reference = _reference(
        runtime,
        f"SELECT s.store_name, {aggregate} FROM jaffle_order o "
        "JOIN jaffle_store s USING (store_id) WHERE o.store_id IN (SELECT store_id "
        "FROM jaffle_order GROUP BY 1 HAVING COUNT(DISTINCT customer_id) >= 4) "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 3",
    )
    actual = [(row["dimension.jaffle_store_name"], row[alias]) for row in rows]
    assert [name for name, _ in actual] == [name for name, _ in reference]
    assert [value for _, value in actual] == pytest.approx([value for _, value in reference])
    assert len(reference) == 2


def test_qualification_not_realizable_drops_to_blocked(runtime) -> None:
    """When the pattern is matched (threshold + entity + target) but no predicate metric
    resolves from the qualification phrase, the candidate drops to blocked with
    QUALIFICATION_NOT_REALIZABLE, never a degenerate draft."""
    payload = plan_candidate_envelope(
        runtime,
        intent="top stores by order count with at least 4 puffins of fluff",
        limit=3,
        verbosity="full",
    )
    blocked_rows = [
        row
        for row in (payload.get("blocked", []) or [])
        if row.get("why_blocked", {}).get("code") == "QUALIFICATION_NOT_REALIZABLE"
    ]
    assert blocked_rows
    # interpreted_intent names the matched pattern with no predicate metric, so the caller
    # knows where the resolution failed.
    interpreted = payload.get("interpreted_intent")
    if isinstance(interpreted, dict):
        assert interpreted.get("pattern") == "qualified_metric_rollup"
        assert interpreted.get("predicate_metrics") == []
    row = blocked_rows[0]
    assert row.get("recovery_hints"), "QUALIFICATION_NOT_REALIZABLE must carry recovery_hints"
    assert "qualification_phrase" in (row["why_blocked"].get("details", {}) or {})


@pytest.mark.parametrize(
    "intent", ["revenue with YoY", "revenue year over year", "revenue vs last year"]
)
def test_a_year_over_year_question_asks_for_the_series(runtime, intent: str) -> None:
    """A comparison with the year before is a question in this release: plan offers the last
    two complete years as a series, ready to send."""
    series = {
        "select": ["measure.jaffle.revenue_usd"],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "year",
            "last": {"unit": "year", "value": 2},
        },
        "order_by": [{"field": "period", "direction": "asc"}],
    }
    plan = plan_payload(runtime, intent=intent)
    assert plan["status"] == "needs_clarification", plan.get("why")
    assert plan["why"]["code"] == "PERIOD_COMPARISON"
    # The one option's call is the drafted series, named by reference.
    [option] = plan["why"]["details"]["options"]
    assert option["call"] == {"$ref": "best.query"}
    assert plan["best"]["query"] == series
    assert runtime.validate(series, now="2026-10-09T12:00:00Z")["ok"]
