"""Authored questions are answered by their certified Query IR, never a fuzzy match."""

from pathlib import Path

import duckdb
import pytest
import yaml

from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime

NOW = {"now": "2026-10-05T06:00:00Z"}
QUESTION = "Which 2 accounts pay the most MRR today?"
ROLE = "temporal_role.subscriptions_account_day_day"
NAME = "dimension.subscriptions_account_name"
DAY = "dimension.subscriptions_account_day_day"
QUERY = {
    "version": 1,
    "select": [{"expression": {"metric": "metric.subscriptions.mrr"}, "as": "mrr"}],
    "group_by": [DAY, NAME],
    "time": {"temporal_role": ROLE, "grain": "day", "range": {"last": {"unit": "day", "value": 1}}},
    "order_by": [{"field": "mrr", "direction": "DESC"}, {"field": NAME, "direction": "ASC"}],
    "limit": 2,
}


def _write_examples(root: Path, entries: dict) -> None:
    path = root / "examples" / "core.yml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump({"examples": entries}), encoding="utf-8")


@pytest.fixture()
def subscriptions(tmp_path):
    root = tmp_path / "subscriptions"
    root.mkdir()
    files = {
        "package.yml": """
schema_version: 1
package:
  id: subscriptions
  namespace: subscriptions
  name: Subscriptions
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
    account_day: {key: [account_id, day], model: account_day}
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
        "models/account_day.yml": """
model:
  id: account_day
  relation: account_day
  entities: {account_day: {}, account: {join: account_id}}
  times:
    day:
      column: day
      kind: date
      class: as_of_time
      default: true
      supported_grains: [day]
  dimensions:
    plan: {kind: categorical, domain: [basic, pro]}
  measures:
    mrr_all:
      expr: mrr
      accumulation: {kind: stock, snapshot: end_of_period}
      publish: false
""",
        "metrics/mrr.yml": """
metrics:
  mrr:
    label: MRR
    kind: semi_additive
    temporal_role: temporal_role.subscriptions_account_day_day
    expression:
      kind: semi_additive
      measure: measure.subscriptions.mrr_all
      filter:
        all: [{field: dimension.subscriptions_account_segment, op: '=', value: customer}]
""",
        "policies/snapshot.yml": """
policies:
  day_required:
    kind: metric_constraint
    object_ids: [measure.subscriptions.mrr_all]
    config:
      required_group_by: [dimension.subscriptions_account_day_day]
""",
    }
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    _write_examples(root, {"top_mrr": {"question": QUESTION, "query": QUERY}})
    with duckdb.connect(str(root / "subscriptions.duckdb")) as conn:
        conn.execute("""
CREATE TABLE accounts (account_id VARCHAR, name VARCHAR, segment VARCHAR);
INSERT INTO accounts VALUES ('a','Acme Data Co','customer'),('b','Globex','customer'),
  ('c','QA Sandbox','internal'),('d','Initech','customer');
CREATE TABLE account_day AS SELECT a.account_id, d::DATE AS day,
  CASE WHEN a.account_id='a' AND d >= DATE '2026-10-02' THEN 'pro' ELSE 'basic' END AS plan,
  CASE WHEN a.account_id='b' AND d >= DATE '2026-10-01' THEN 0
       WHEN a.account_id='a' AND d >= DATE '2026-10-02' THEN 500 ELSE 99 END::DOUBLE AS mrr
  FROM accounts a, range(DATE '2026-09-01', DATE '2026-10-05', INTERVAL 1 DAY) t(d);
""")
    runtime = Runtime.from_path(str(root))
    try:
        yield runtime
    finally:
        runtime.close()


def _plan(runtime, question=QUESTION, **kwargs):
    return plan_payload(runtime, intent=question, partial_query={"policy_context": NOW}, **kwargs)


def _reference(runtime, day, limit):
    with duckdb.connect(runtime.db_path, read_only=True) as conn:
        return conn.execute(
            "SELECT a.name, d.mrr FROM account_day d JOIN accounts a USING(account_id) "
            "WHERE a.segment = 'customer' AND d.day = ? ORDER BY d.mrr DESC, a.name LIMIT ?",
            [day, limit],
        ).fetchall()


def test_authored_snapshot_question_answers_reference(subscriptions):
    result = _plan(subscriptions)
    assert result["status"] == "ok", result.get("why")
    assert result["best"]["pattern"] == "package_example"
    rows = subscriptions.query(result["best"]["query_ir"])["rows"]
    gold = _reference(subscriptions, "2026-10-04", 2)
    assert gold == [("Acme Data Co", 500), ("Initech", 99)]
    assert [(row[NAME], row["mrr"]) for row in rows] == gold
