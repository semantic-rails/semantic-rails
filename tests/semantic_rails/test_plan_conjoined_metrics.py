"""Exact compound subjects share one query only when their scopes agree."""

import json
from dataclasses import replace
from pathlib import Path

import duckdb
import pytest
import yaml

from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.conftest import write_single_file_package
from tests.semantic_rails.result_helpers import disable_planner_patterns

NOW = {"now": "2026-10-05T06:00:00Z"}
EVENT_CLOCK = "temporal_role.subscriptions_event_occurred_at"
DAY_CLOCK = "temporal_role.subscriptions_account_day_day"


@pytest.fixture()
def subscriptions(tmp_path: Path):
    files = {
        "package.yml": """
schema_version: 1
package:
  id: subscriptions
  namespace: subscriptions
  name: Subscriptions
  description: Account activity
  warehouse: duckdb
  default_db: subscriptions.duckdb
  seed: {kind: external}
defaults:
  time: {timezone: UTC}
""",
        "graph.yml": """
graph:
  entities:
    account: {key: [account_id], model: accounts}
    event: {key: [event_id], model: events}
    account_day: {key: [account_id, day], model: account_day}
  relationships:
    event_account: {entities: [event, account], cardinality: many_to_one}
    day_account: {entities: [account_day, account], cardinality: many_to_one}
""",
        "models/accounts.yml": """
model:
  id: accounts
  relation: accounts
  entities: {account: {}}
  dimensions:
    name: {kind: categorical}
    segment: {kind: categorical, domain: [customer, internal]}
""",
        "models/events.yml": """
model:
  id: events
  relation: events
  entities: {event: {}, account: {column: account_id}}
  times:
    occurred_at: {column: occurred_at, kind: date, class: event_time, default: true}
  dimensions:
    kind: {kind: categorical, domain: [signup, close, upgrade]}
  measures:
    events_all: {kind: entity_count, entity_key: event_id, value_type: count, publish: false}
""",
        "models/account_day.yml": """
model:
  id: account_day
  relation: account_day
  entities: {account_day: {}, account: {column: account_id}}
  times:
    day: {column: day, kind: date, class: as_of_time, default: true}
  dimensions:
    plan: {kind: categorical, domain: [basic, pro]}
  measures:
    recurring_balance:
      expr: mrr
      accumulation: {kind: stock, snapshot: end_of_period}
      publish: false
""",
    }
    metrics = {
        key: {
            "label": label,
            "kind": "aggregate",
            "value_type": "count",
            "temporal_role": EVENT_CLOCK,
            "expression": {
                "kind": "aggregate",
                "measure": "measure.subscriptions.events_all",
                "aggregation": "count_distinct",
                "filter": {
                    "all": [
                        {"field": "dimension.subscriptions_event_kind", "op": "=", "value": kind},
                        {
                            "field": "dimension.subscriptions_account_segment",
                            "op": "=",
                            "value": "customer",
                        },
                    ]
                },
            },
        }
        for key, label, kind in [
            ("new_accounts", "New accounts", "signup"),
            ("closures", "Closures", "close"),
            ("upgrades", "Upgrades", "upgrade"),
        ]
    }
    metrics["new_accounts"]["synonyms"] = ["Signups"]
    metrics["mrr"] = {
        "label": "MRR (USD)",
        "kind": "semi_additive",
        "temporal_role": DAY_CLOCK,
        "expression": {
            "kind": "semi_additive",
            "measure": "measure.subscriptions.recurring_balance",
        },
    }
    files["metrics/accounts.yml"] = yaml.safe_dump({"metrics": metrics})
    for name, contents in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    with duckdb.connect(str(tmp_path / "subscriptions.duckdb")) as connection:
        connection.execute("""
CREATE TABLE accounts (account_id VARCHAR, name VARCHAR, segment VARCHAR);
INSERT INTO accounts VALUES ('a','Acme Data Co','customer'),('b','Globex','customer'),
  ('c','QA Sandbox','internal'),('d','Initech','customer');
CREATE TABLE events (event_id INTEGER, account_id VARCHAR, kind VARCHAR, occurred_at DATE);
INSERT INTO events VALUES (1,'a','signup','2026-09-02'),(2,'b','signup','2026-09-22'),
  (3,'c','signup','2026-09-29'),(4,'d','signup','2026-09-30'),
  (5,'b','close','2026-10-01'),(6,'a','upgrade','2026-10-02'),
  (7,'c','signup','2026-09-28'),(8,'c','signup','2026-10-03'),(9,'c','signup','2026-10-04');
CREATE TABLE account_day (account_id VARCHAR, day DATE, plan VARCHAR, mrr DOUBLE);
INSERT INTO account_day SELECT a.account_id, d::DATE,
  CASE WHEN a.account_id='a' AND d >= DATE '2026-10-02' THEN 'pro' ELSE 'basic' END,
  CASE WHEN a.account_id='b' AND d >= DATE '2026-10-01' THEN 0
       WHEN a.account_id='a' AND d >= DATE '2026-10-02' THEN 500 ELSE 99 END
  FROM accounts a, range(DATE '2026-09-01', DATE '2026-10-05', INTERVAL 1 DAY) t(d);
""")
    runtime = Runtime.from_path(str(tmp_path))
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("question", "kinds"),
    [
        ("New accounts and closures last week", ["signup", "close"]),
        ("New accounts, closures and upgrades last week", ["signup", "close", "upgrade"]),
        ("Closures and new accounts last week", ["close", "signup"]),
        ("Signups and closures last week", ["signup", "close"]),
        ("New accounts and closures by segment last week", ["signup", "close"]),
        ("New accounts and closures where segment is customer last week", ["signup", "close"]),
    ],
)
def test_shared_clock_executes_every_subject(subscriptions, question, kinds):
    payload = plan_payload(subscriptions, intent=question, partial_query={"policy_context": NOW})
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query_ir"]
    assert query["version"] == 1
    assert payload["best"]["pattern"] == "conjoined_metrics"
    assert len(query["select"]) == len(kinds)
    for part in payload["best"]["interpreted_intent"]["parts"]:
        assert part["spans"]
        for start, end in part["spans"]:
            assert question[start:end].lower() in part["phrase"]
    rows = subscriptions.query(query)["rows"]
    grouped = "by segment" in question
    filtered = "where segment" in question
    aggregates = ", ".join(
        "COUNT(DISTINCT CASE WHEN a.segment = 'customer' AND e.kind = ? THEN e.event_id END)"
        for _ in kinds
    )
    sql = (
        "SELECT "
        + ("a.segment, " if grouped else "")
        + aggregates
        + " FROM events e JOIN accounts a USING (account_id) "
        + "WHERE e.occurred_at >= DATE '2026-09-28' AND e.occurred_at < DATE '2026-10-05'"
        + (" AND a.segment = 'customer'" if filtered else "")
        + (" GROUP BY a.segment ORDER BY a.segment" if grouped else "")
    )
    with duckdb.connect(subscriptions.db_path, read_only=True) as connection:
        reference = connection.execute(sql, kinds).fetchall()
    actual = [
        tuple(
            ([row["dimension.subscriptions_account_segment"]] if grouped else [])
            + [row[item["as"]] for item in query["select"]]
        )
        for row in rows
    ]
    assert sorted(actual) == reference
    assert reference == (
        [tuple(["customer", *([1] * len(kinds))]), tuple(["internal", *([0] * len(kinds))])]
        if grouped
        else [tuple([1] * len(kinds))]
    )


def test_different_clocks_are_held_with_parts(subscriptions):
    payload = plan_payload(
        subscriptions,
        intent="New accounts and MRR last week",
        partial_query={"policy_context": NOW},
    )
    assert payload["status"] == "low_confidence"
    assert "execute" not in payload.get("next", {}).get("ready_for", [])
    assert "multiple_subjects_unrealized" in str(payload["why"])
    assert payload["best"]["pattern"] == "conjoined_metrics"
    assert [part["temporal_roles"] for part in payload["why"]["details"]["parts"]] == [
        [EVENT_CLOCK],
        [DAY_CLOCK],
    ]


def test_disabled_pattern_keeps_missing_subject_guard(subscriptions):
    disable_planner_patterns(subscriptions, "conjoined_metrics")
    payload = plan_payload(
        subscriptions,
        intent="New accounts and closures last week",
        partial_query={"policy_context": NOW},
    )
    assert payload["status"] == "low_confidence"
    assert "multiple_subjects_unrealized" in str(payload["why"])


def test_ambiguous_piece_clarifies(subscriptions):
    metrics = subscriptions._config.metric_recipes
    new = next(row for row in metrics if row.id.endswith(".new_accounts"))
    closures = next(row for row in metrics if row.id.endswith(".closures"))
    subscriptions._config = replace(
        subscriptions._config,
        metric_recipes=[
            *metrics,
            replace(
                new, id="metric.subscriptions.other_new_accounts", expression=closures.expression
            ),
        ],
    )
    payload = plan_payload(
        subscriptions,
        intent="New accounts and closures last week",
        partial_query={"policy_context": NOW},
    )
    assert payload["status"] == "needs_clarification", payload.get("why")
    assert payload["why"]["details"]["gaps"][0]["kind"] == "subject_ambiguous"
    assert payload["why"]["details"]["gaps"][0]["expected"]["candidates"] == [
        "metric.subscriptions.new_accounts",
        "metric.subscriptions.other_new_accounts",
    ]


def test_caller_grain_is_validated_after_the_merge(subscriptions):
    subscriptions._config = replace(
        subscriptions._config,
        temporal_roles=[
            replace(row, supported_grains=["day"]) for row in subscriptions._config.temporal_roles
        ],
    )
    payload = plan_payload(
        subscriptions,
        intent="New accounts and closures last week",
        partial_query={"time": {"grain": "day"}, "policy_context": NOW},
    )
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query_ir"]
    assert payload["best"]["pattern"] == "conjoined_metrics"
    rows = subscriptions.query(query)["rows"]
    with duckdb.connect(subscriptions.db_path, read_only=True) as connection:
        reference = connection.execute("""
SELECT d::DATE,
  COUNT(DISTINCT CASE WHEN a.segment = 'customer' AND e.kind = 'signup' THEN e.event_id END),
  COUNT(DISTINCT CASE WHEN a.segment = 'customer' AND e.kind = 'close' THEN e.event_id END)
FROM range(DATE '2026-09-28', DATE '2026-10-05', INTERVAL 1 DAY) days(d)
LEFT JOIN events e ON e.occurred_at = d
LEFT JOIN accounts a USING (account_id)
GROUP BY 1 ORDER BY 1
""").fetchall()
    assert len(reference) == 7
    assert [
        (str(row[f"{EVENT_CLOCK}__day"])[:10], *[row[item["as"]] for item in query["select"]])
        for row in rows
    ] == [(str(day), new, closed) for day, new, closed in reference]


def test_customer_count_uses_its_named_metric(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="customer count and lifetime order count by month")
        assert payload["status"] == "ok", payload.get("why")
        query = payload["best"]["query_ir"]
        assert [item["expression"] for item in query["select"]] == [
            {"metric": "metric.sales.customer_count"},
            {"measure": "measure.jaffle.lifetime_order_count", "aggregation": "sum"},
        ]
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            reference = connection.execute("""
SELECT DATE_TRUNC('month', first_order_at), COUNT(DISTINCT customer_id), SUM(lifetime_order_count)
FROM jaffle_customer GROUP BY 1 ORDER BY 1
""").fetchall()
        assert [
            (
                str(row["temporal_role.jaffle_customer_first_order_at__month"])[:10],
                *[row[item["as"]] for item in query["select"]],
            )
            for row in runtime.query(query)["rows"]
        ] == [(str(month)[:10], count, lifetime) for month, count, lifetime in reference]
    finally:
        runtime.close()


@pytest.mark.parametrize("publish", [True, False])
def test_starter_revenue_uses_its_named_metric(tmp_path, publish):
    package = write_single_file_package(tmp_path / "starter")
    raw = yaml.safe_load(package.read_text())
    raw["models"]["orders"]["measures"]["revenue_usd"]["publish"] = publish
    package.write_text(yaml.safe_dump(raw))
    runtime = Runtime.from_path(str(package))
    try:
        payload = plan_payload(runtime, intent="revenue and order count by month")
        assert payload["status"] == "ok", payload.get("why")
        query = payload["best"]["query_ir"]
        assert query["select"][0]["expression"] == {"metric": "metric.shop.revenue_usd"}
        assert query["select"][1]["expression"]["measure"] == "measure.shop.order_count"
        rows = runtime.query(query)["rows"]
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            reference = connection.execute("""
SELECT DATE_TRUNC('month', ordered_at), SUM(order_total_cents / 100.0), COUNT(DISTINCT order_id)
FROM shop_order GROUP BY 1 ORDER BY 1
""").fetchall()
        assert [
            (
                str(row["temporal_role.shop_order_ordered_at__month"])[:10],
                *[float(row[item["as"]]) for item in query["select"]],
            )
            for row in rows
        ] == [(str(month)[:10], float(revenue), count) for month, revenue, count in reference]
    finally:
        runtime.close()


@pytest.mark.parametrize("compatible", [False, True])
def test_each_subject_uses_its_own_clock(subscriptions, compatible):
    # Changing a metric's clock must not fall back to its measure's event clock.
    metrics = subscriptions._config.metric_recipes
    subscriptions._config = replace(
        subscriptions._config,
        metric_recipes=[
            replace(
                row,
                temporal_role=DAY_CLOCK,
                compatible_temporal_roles=[EVENT_CLOCK] if compatible else [DAY_CLOCK],
            )
            if row.id.endswith(".closures")
            else row
            for row in metrics
        ],
    )
    if compatible:
        test_shared_clock_executes_every_subject(
            subscriptions, "New accounts and closures last week", ["signup", "close"]
        )
    else:
        payload = plan_payload(
            subscriptions,
            intent="New accounts and closures last week",
            partial_query={"policy_context": NOW},
        )
        assert payload["status"] == "low_confidence"
        assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
        assert [part["temporal_roles"] for part in payload["why"]["details"]["parts"]] == [
            [EVENT_CLOCK],
            [DAY_CLOCK],
        ]


def test_an_unsupported_shared_window_is_held(subscriptions):
    subscriptions._config = replace(
        subscriptions._config,
        temporal_roles=[
            replace(row, supported_grains=["day"]) for row in subscriptions._config.temporal_roles
        ],
    )
    payload = plan_payload(
        subscriptions,
        intent="New accounts and closures last week",
        partial_query={"policy_context": NOW},
    )
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "VALIDATION_FAILED"
    assert "execute" not in payload.get("next", {}).get("ready_for", [])


def test_a_later_filter_value_is_not_consumed_as_a_subject(subscriptions):
    payload = plan_payload(
        subscriptions,
        intent="New accounts and upgrades where kind is upgrade last week",
        partial_query={"policy_context": NOW},
    )
    assert payload["status"] == "low_confidence", payload.get("why")
    assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert payload["why"]["details"]["gaps"][0]["kind"] == "multiple_subjects_unrealized"
    assert "execute" not in payload.get("next", {}).get("ready_for", [])
    # The later occurrence was retained as a filter in the first subject's query.
    assert payload["best"]["query_ir"]["where"] == [
        {"field": "dimension.subscriptions_event_kind", "op": "=", "value": "upgrade"}
    ]


@pytest.mark.parametrize(
    ("question", "subjects", "grain", "start", "end"),
    [
        ("revenue and orders by month", ["revenue_usd", "order_count"], "month", None, None),
        (
            "What is order count and revenue by month?",
            ["order_count", "revenue_usd"],
            "month",
            None,
            None,
        ),
        (
            "how many orders and revenue by month",
            ["order_count", "revenue_usd"],
            "month",
            None,
            None,
        ),
        (
            "item revenue and orders in Q1 2017",
            ["item_revenue_usd", "order_count"],
            "quarter",
            "2017-01-01",
            "2017-04-01",
        ),
        (
            "revenue and item revenue in 2017",
            ["revenue_usd", "item_revenue_usd"],
            "year",
            "2017-01-01",
            "2018-01-01",
        ),
        (
            "gross profit and revenue for the first half of 2017",
            ["gross_profit_usd", "revenue_usd"],
            "year",
            "2017-01-01",
            "2017-07-01",
        ),
        (
            "orders and revenue in March 2017",
            ["order_count", "revenue_usd"],
            "month",
            "2017-03-01",
            "2017-04-01",
        ),
        (
            "revenue, orders and gross profit from March 1 to March 31, 2017",
            ["revenue_usd", "order_count", "gross_profit_usd"],
            "month",
            "2017-03-01",
            "2017-04-01",
        ),
    ],
)
def test_conjoined_measures_match_independent_sql(
    runtime_factory, question, subjects, grain, start, end
):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=question)
        assert payload["status"] == "ok", payload.get("why")
        query = payload["best"]["query_ir"]
        assert [item["expression"]["measure"] for item in query["select"]] == [
            f"measure.jaffle.{subject}" for subject in subjects
        ]
        assert query["time"]["grain"] == grain
        rows = runtime.query(query)["rows"]
        gold = {
            "revenue_usd": "SUM(o.order_total_cents / 100.0)",
            "order_count": "COUNT(DISTINCT o.order_id)",
            "gross_profit_usd": "SUM(o.gross_profit_cents / 100.0)",
            "item_revenue_usd": "SUM(i.item_revenue_cents / 100.0)",
        }
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            for subject, item in zip(subjects, query["select"], strict=True):
                sql = f"SELECT DATE_TRUNC('{grain}', o.ordered_at), {gold[subject]} FROM jaffle_order o"
                if subject == "item_revenue_usd":
                    sql += " JOIN jaffle_item i ON i.order_id = o.order_id"
                params = []
                if start:
                    sql += " WHERE o.ordered_at >= CAST(? AS TIMESTAMP) AND o.ordered_at < CAST(? AS TIMESTAMP)"
                    params = [start, end]
                sql += " GROUP BY 1 ORDER BY 1"
                reference = {
                    str(bucket)[:10]: value
                    for bucket, value in connection.execute(sql, params).fetchall()
                }
                actual = {
                    str(row[f"temporal_role.jaffle_order_time__{grain}"])[:10]: float(
                        row[item["as"]]
                    )
                    for row in rows
                }
                assert actual == pytest.approx(reference)
    finally:
        runtime.close()


def test_another_longer_subject_cannot_hide_a_pieces_ambiguity(subscriptions):
    metrics = subscriptions._config.metric_recipes
    new = next(row for row in metrics if row.id.endswith(".new_accounts"))
    subscriptions._config = replace(
        subscriptions._config,
        metric_recipes=[
            *[
                replace(row, label="Closed new accounts") if row.id.endswith(".closures") else row
                for row in metrics
            ],
            replace(new, id="metric.subscriptions.other_new_accounts"),
        ],
    )
    payload = plan_payload(
        subscriptions,
        intent="New accounts and closed new accounts last week",
        partial_query={"policy_context": NOW},
    )
    assert payload["status"] == "needs_clarification"
    assert payload["why"]["details"]["gaps"][0]["expected"]["candidates"] == [
        "metric.subscriptions.new_accounts",
        "metric.subscriptions.other_new_accounts",
    ]


@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
def test_a_hidden_piece_stays_unknown_and_never_discloses_its_id(subscriptions, detail):
    hidden = "metric.subscriptions.closures"
    policy = SemanticPolicyConfig(
        id="policy.hide_closures",
        kind="object_visibility",
        object_ids=[hidden],
        action="hidden",
        audiences=["external"],
    )
    subscriptions._config = replace(subscriptions._config, semantic_policies=[policy])
    payload = plan_payload(
        subscriptions,
        intent="New accounts and closures last week",
        detail=detail,
        partial_query={"policy_context": {**NOW, "audience": "external"}},
    )
    assert payload["status"] == "low_confidence", payload.get("why")
    assert hidden not in json.dumps(payload)
    assert "execute" not in payload.get("next", {}).get("ready_for", [])
