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

from semantic_rails.planner import plan_payload
from tests.plan_candidate_envelope import plan_candidate_envelope


def _reference(runtime, sql: str) -> list[tuple]:
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        return connection.execute(sql).fetchall()


def test_predicate_metric_resolves_to_customer_count_not_aov(runtime_factory) -> None:
    """The reviewer's flagship failure: 'with at least N distinct
    customers' silently resolved ``predicate_metrics`` to
    ``metric.sales.aov_usd``. After phase 5 it must resolve to one of
    the customer-count measures/metrics — never AOV.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(
            runtime,
            intent="top stores by order count with at least 4 distinct customers",
            limit=5,
        )
        assert payload["interpreted_intent"]["pattern"] == "qualified_metric_rollup"
        predicate_metrics = list(payload["interpreted_intent"].get("predicate_metrics", []) or [])
        assert predicate_metrics, (
            "predicate_metrics must resolve — without a predicate the qualification is unrealizable"
        )
        # Never AOV — that was the bug.
        assert "metric.sales.aov_usd" not in predicate_metrics
        # Must be one of the customer-count primitives.
        allowed = {
            "measure.jaffle.ordering_customer_count",
            "measure.jaffle.customer_count",
            "metric.sales.customer_count",
        }
        assert set(predicate_metrics) & allowed, (
            f"expected a customer-count predicate; got {predicate_metrics}"
        )
    finally:
        runtime.close()


def test_a_top_n_qualified_rollup_ranks_the_qualifying_entities(runtime_factory) -> None:
    """No window: every order of each store qualifies it, the one reading, so plan answers."""
    runtime = runtime_factory("jaffle_shop")
    try:
        plan = plan_payload(
            runtime, intent="top 3 stores by order count with at least 4 distinct customers"
        )
        assert plan["status"] == "ok", plan.get("why")
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
        assert query["order_by"] == [{"field": "order_count", "direction": "desc"}]
        assert query["limit"] == 3
        rows = runtime.query(query)["rows"]
        reference = _reference(
            runtime,
            "SELECT s.store_name, COUNT(DISTINCT o.order_id) FROM jaffle_order o "
            "JOIN jaffle_store s USING (store_id) WHERE o.store_id IN (SELECT store_id "
            "FROM jaffle_order GROUP BY 1 HAVING COUNT(DISTINCT customer_id) >= 4) "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 3",
        )
    finally:
        runtime.close()
    assert [(row["dimension.jaffle_store_name"], row["order_count"]) for row in rows] == reference
    assert len(reference) == 2


def test_a_qualified_rollup_in_each_period_returns_every_row(runtime_factory) -> None:
    """Without a "top N" request, a default limit would silently drop every month after the
    first few, so the call has none; "in that month" qualifies each customer per period."""
    runtime = runtime_factory("jaffle_shop")
    try:
        plan = plan_payload(
            runtime,
            intent="monthly order volume for customers that made more than 10 purchases in that month",
        )
        assert plan["status"] == "ok", plan.get("why")
        query = plan["best"]["query"]
        assert "limit" not in query
        assert query["where"] == [
            {
                "entity": "entity.jaffle_customer",
                "having": [{"field": "measure.jaffle.order_count", "op": "gt", "value": 10}],
                "over": "period",
            }
        ]
        rows = runtime.query(query)["rows"]
        reference = _reference(
            runtime,
            "WITH monthly AS (SELECT customer_id, DATE_TRUNC('month', ordered_at) AS m, "
            "COUNT(DISTINCT order_id) AS n FROM jaffle_order GROUP BY 1, 2) "
            "SELECT CAST(DATE_TRUNC('month', o.ordered_at) AS DATE)::VARCHAR, "
            "COUNT(DISTINCT o.order_id) FROM jaffle_order o JOIN monthly c "
            "ON c.customer_id = o.customer_id AND c.m = DATE_TRUNC('month', o.ordered_at) "
            "WHERE c.n > 10 GROUP BY 1 ORDER BY 1",
        )
    finally:
        runtime.close()
    assert [(row["period"], row["order_count"]) for row in rows] == reference
    assert len(reference) > 2


def test_qualification_not_realizable_drops_to_blocked(runtime_factory) -> None:
    """When the pattern is matched (threshold + entity + target) but no
    predicate metric can be resolved from the qualification phrase, the
    candidate must drop to blocked with QUALIFICATION_NOT_REALIZABLE.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        # An intent whose qualification phrase has NO catalog-tokens
        # that resolve to any measure or metric. The threshold is
        # present, the entity is present, the target is present —
        # only the predicate target can't be resolved.
        payload = plan_candidate_envelope(
            runtime,
            intent="top stores by order count with at least 4 puffins of fluff",
            limit=3,
            verbosity="full",
        )
        # The qualified path should mark this as blocked. Note that
        # discovery may still surface unrelated fallback candidates —
        # what matters is that the qualified pattern itself did NOT
        # silently produce a degenerate IR.
        blocked_codes = {
            row.get("why_blocked", {}).get("code") for row in (payload.get("blocked", []) or [])
        }
        assert "QUALIFICATION_NOT_REALIZABLE" in blocked_codes, (
            f"expected QUALIFICATION_NOT_REALIZABLE in blocked codes; got {blocked_codes}"
        )
        # interpreted_intent still names the matched pattern with
        # predicate_metrics = [] so the caller knows where the
        # resolution failed.
        ii = payload.get("interpreted_intent")
        if isinstance(ii, dict):
            assert ii.get("pattern") == "qualified_metric_rollup"
            assert ii.get("predicate_metrics") == []
        # The blocked envelope carries recovery hints naming the
        # qualification phrase that couldn't be resolved.
        blocked_rows = [
            row
            for row in (payload.get("blocked", []) or [])
            if row.get("why_blocked", {}).get("code") == "QUALIFICATION_NOT_REALIZABLE"
        ]
        assert blocked_rows
        row = blocked_rows[0]
        assert row.get("recovery_hints"), "QUALIFICATION_NOT_REALIZABLE must carry recovery_hints"
        details = row["why_blocked"].get("details", {}) or {}
        assert "qualification_phrase" in details
    finally:
        runtime.close()


def test_a_year_over_year_question_asks_for_the_series(runtime_factory) -> None:
    """A comparison with the year before is a question in this release: plan offers the last
    two complete years as a series, ready to send."""
    runtime = runtime_factory("jaffle_shop")
    series = {
        "select": ["measure.jaffle.revenue_usd"],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "year",
            "last": {"unit": "year", "value": 2},
        },
        "order_by": [{"field": "period", "direction": "asc"}],
    }
    try:
        for intent in ("revenue with YoY", "revenue year over year", "revenue vs last year"):
            plan = plan_payload(runtime, intent=intent)
            assert plan["status"] == "needs_clarification", (intent, plan.get("why"))
            assert plan["why"]["code"] == "PERIOD_COMPARISON", intent
            # The one option's call is the drafted series, named by reference.
            [option] = plan["why"]["details"]["options"]
            assert option["call"] == {"$ref": "best.query"}, intent
            assert plan["best"]["query"] == series, intent
            assert runtime.validate(series, now="2026-10-09T12:00:00Z")["ok"]
    finally:
        runtime.close()
