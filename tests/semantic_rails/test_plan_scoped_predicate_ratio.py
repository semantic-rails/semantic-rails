"""A drafted share whose predicates are measured on another clock is not offered as ready.

The anchor (recurring revenue) is a monthly snapshot; the qualifying metrics (SMS and push
messages sent) are events on their own clock. Matching the two by calendar month is a
different question than the user asked, so the draft must not opt into it: the engine
refuses the cross-clock predicate and the plan does not offer the draft as ready.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from semantic_rails.planner._base import _metric_predicate
from semantic_rails.runtime import Runtime
from tests.plan_candidate_envelope import plan_candidate_envelope

SEED_SQL = """
CREATE TABLE accounts (account_id INTEGER, snapshot_at TIMESTAMP, arr INTEGER);
CREATE TABLE messages (message_id INTEGER, account_id INTEGER, sent_at TIMESTAMP,
                       sms_sent INTEGER, push_sent INTEGER);
INSERT INTO accounts VALUES
  (1, TIMESTAMP '2025-01-31 00:00:00', 100), (2, TIMESTAMP '2025-01-31 00:00:00', 50);
INSERT INTO messages VALUES
  (1, 1, TIMESTAMP '2025-01-10 00:00:00', 3, 2), (2, 2, TIMESTAMP '2025-03-10 00:00:00', 1, 0);
"""


def _model(model_id: str, entities: list[str], time: tuple[str, str], measures: str) -> str:
    column, role = time
    entity_lines = "\n".join(f"    {name}: {{}}" for name in entities)
    return (
        f"model:\n  id: {model_id}\n  relation: {model_id}\n  entities:\n{entity_lines}\n"
        f"  times:\n    {column}:\n      label: {column}\n      column: {column}\n"
        f"      kind: timestamp\n      class: event_time\n      as: temporal_role.plan_{role}\n"
        f"      default: true\n      default_query_axis: true\n  measures:\n{measures}"
    )


def _flow(name: str, expr: str) -> str:
    return (
        f"    {name}:\n      label: {name}\n      kind: aggregate\n      expr: {expr}\n"
        "      accumulation: {kind: flow}\n      value_type: count\n"
    )


@pytest.fixture()
def runtime(tmp_path: Path):
    root = tmp_path / "plan"
    (root / "data").mkdir(parents=True)
    (root / "models").mkdir()
    (root / "data" / "seed.sql").write_text(SEED_SQL)
    (root / "package.yml").write_text(
        textwrap.dedent(
            """
            schema_version: 1
            package:
              id: plan
              namespace: plan
              name: plan
              description: Fixture for cross-clock plan drafts.
              warehouse: duckdb
              default_db: data/plan.duckdb
              seed: {kind: sql_script, source: data/seed.sql}
            """
        )
    )
    (root / "graph.yml").write_text(
        "graph:\n  entities:\n"
        "    account: {label: Account, key: [account_id], model: accounts}\n"
        "    message: {label: Message, key: [message_id], model: messages}\n"
    )
    (root / "models" / "accounts.yml").write_text(
        _model("accounts", ["account"], ("snapshot_at", "snapshot_at"), _flow("arr", "arr"))
    )
    (root / "models" / "messages.yml").write_text(
        _model(
            "messages",
            ["message", "account"],
            ("sent_at", "sent_at"),
            _flow("sms_sent", "sms_sent") + _flow("push_sent", "push_sent"),
        )
    )
    runtime = Runtime.from_path(str(root))
    try:
        yield runtime
    finally:
        runtime.close()


def test_a_drafted_metric_predicate_never_asks_for_calendar_alignment():
    metric = type("Metric", (), {"id": "metric.plan.sms_sent"})()
    entity = type("Entity", (), {"id": "entity.plan_account"})()
    assert "time_alignment" not in _metric_predicate(metric, entity)


def test_a_share_drafted_across_clocks_is_not_offered_as_ready(runtime):
    result = plan_candidate_envelope(
        runtime, intent="share of arr from sms and push senders by month", limit=3
    )
    assert "same_query_period" not in json.dumps(result)
    ratios = [
        candidate
        for candidate in result["candidates"]
        if "predicates" in json.dumps(candidate["candidate_ir"])
    ]
    assert ratios, "the share draft should still be shown, just not as ready"
    assert all(candidate["validation"]["ok"] is False for candidate in ratios)
    assert {
        error["code"] for candidate in ratios for error in candidate["validation"]["errors"]
    } == {"INVALID_TEMPORAL_BINDING"}
