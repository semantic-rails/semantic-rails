"""Planning uses the caller's clock and holds unconsumed as-of cues."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.planner._base import _time_window
from semantic_rails.planner.faithfulness import _window_agrees
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.orchestrator import compose
from semantic_rails.runtime import Runtime

NOW = {"now": "2026-10-05T06:00:00Z"}
SEED = """
CREATE TABLE events (event_id INTEGER, kind VARCHAR, occurred_at DATE);
INSERT INTO events VALUES (1, 'signup', '2026-10-05'), (2, 'signup', '2026-10-05'),
  (3, 'signup', '2026-10-06'), (4, 'close', '2026-10-05');
CREATE TABLE account_day (account_id VARCHAR, day DATE, mrr DOUBLE);
INSERT INTO account_day VALUES ('a', '2026-10-04', 99), ('b', '2026-10-04', 500);
"""


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
    event: {key: [event_id], model: events}
    account_day: {key: [account_id, day], model: account_day}
""",
        "models/events.yml": """
model:
  id: events
  relation: events
  entities: {event: {}}
  times:
    occurred_at: {column: occurred_at, kind: date, class: event_time, default: true}
  dimensions:
    kind: {kind: categorical, domain: [signup, close]}
  measures:
    events_all:
      kind: entity_count
      entity_key: event_id
      value_type: count
      publish: false
""",
        "models/account_day.yml": """
model:
  id: account_day
  relation: account_day
  entities: {account_day: {}}
  times:
    day: {column: day, kind: date, class: as_of_time, default: true}
  measures:
    mrr_all:
      expr: mrr
      accumulation: {kind: stock, snapshot: end_of_period}
      publish: false
""",
        "metrics/accounts.yml": """
metrics:
  new_accounts:
    label: New accounts
    kind: aggregate
    value_type: count
    temporal_role: temporal_role.subscriptions_event_occurred_at
    expression:
      kind: aggregate
      measure: measure.subscriptions.events_all
      aggregation: count_distinct
      filter:
        all: [{field: dimension.subscriptions_event_kind, op: '=', value: signup}]
  mrr:
    label: MRR
    kind: semi_additive
    temporal_role: temporal_role.subscriptions_account_day_day
    expression:
      kind: semi_additive
      measure: measure.subscriptions.mrr_all
""",
    }
    for name, contents in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    with duckdb.connect(str(tmp_path / "subscriptions.duckdb")) as connection:
        connection.execute(SEED)
    runtime = Runtime.from_path(str(tmp_path))
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.fixture()
def wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    class MachineDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2031, 2, 12)

    monkeypatch.setattr("semantic_rails.planner._base.date", MachineDate)


@pytest.mark.parametrize(
    ("phrase", "start", "end"),
    [
        ("today", "2026-10-05", "2026-10-06"),
        ("this day", "2026-10-05", "2026-10-06"),
        ("this week", "2026-10-05", "2026-10-12"),
        ("this month", "2026-10-01", "2026-11-01"),
        ("this quarter", "2026-10-01", "2027-01-01"),
        ("this year", "2026-01-01", "2027-01-01"),
    ],
)
def test_current_windows_use_the_query_clock(
    subscriptions: Runtime, wall_clock: None, phrase: str, start: str, end: str
) -> None:
    plan = plan_payload(
        subscriptions, intent=f"new accounts {phrase}", partial_query={"policy_context": NOW}
    )
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query_ir"]
    assert (query["time"]["start"], query["time"]["end"]) == (start, end)
    rows = subscriptions.query({**query, "policy_context": NOW})["rows"]
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        gold = connection.execute(
            "SELECT COUNT(DISTINCT event_id) FROM events "
            "WHERE kind = 'signup' AND occurred_at >= ? AND occurred_at < ?",
            [start, end],
        ).fetchone()[0]
    assert sum(row[query["select"][0]["as"]] for row in rows) == gold


@pytest.mark.parametrize("subject", ["new accounts", "MRR"])
@pytest.mark.parametrize(
    "phrase",
    [
        "now",
        "right now",
        "currently",
        "at the moment",
        "as of now",
        "current",
        "at the end of last month",
        "end of last month",
        "as of last month",
        "as of September 2026",
        "as of 2026-09-30",
    ],
)
def test_as_of_cues_are_held_in_full(subscriptions: Runtime, subject: str, phrase: str) -> None:
    plan = plan_payload(
        subscriptions, intent=f"{subject} {phrase}", partial_query={"policy_context": NOW}
    )
    assert plan["status"] == "low_confidence", plan
    assert plan["why"]["code"] == "TIME_WINDOW_UNRESOLVED", plan.get("why")
    assert plan["why"]["details"]["unresolved_phrases"] == [phrase.lower()]
    assert not plan["next"].get("ready_for")
    assert not (plan.get("best") or {}).get("query_ir")


def test_current_before_a_metric_is_an_as_of_cue(subscriptions: Runtime) -> None:
    plan = plan_payload(subscriptions, intent="current new accounts")
    assert plan["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
    assert plan["why"]["details"]["unresolved_phrases"] == ["current"]


def test_as_of_window_is_not_partially_resolved() -> None:
    text = "new accounts at the end of last month"
    window = _time_window(text)
    assert window.bounds == {}
    assert window.windows == ()
    assert window.unresolved == ("at the end of last month",)
    assert window.as_of[0].kind == "closing_day"
    assert text[slice(*window.as_of[0].span)] == "at the end of last month"


@pytest.mark.parametrize("phrase", ["now", "right now", "end of last month"])
def test_an_explicit_interval_does_not_consume_an_as_of_cue(
    subscriptions: Runtime, phrase: str
) -> None:
    plan = plan_payload(
        subscriptions,
        intent=f"new accounts {phrase}",
        partial_query={
            "policy_context": NOW,
            "time": {"start": "2026-10-04", "end": "2026-10-05"},
        },
    )
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
    assert not (plan.get("best") or {}).get("query_ir")


def test_the_clock_is_scoped_and_the_cache_is_keyed_by_date(
    subscriptions: Runtime, wall_clock: None
) -> None:
    for context, expected in [(NOW, "2026-10-05"), ({"now": "2026-10-06"}, "2026-10-06")]:
        parsed = parse_intent(subscriptions, "new accounts today", policy_context=context)
        composed = compose(subscriptions, "new accounts today", policy_context=context)
        assert parsed.time["start"] == expected
        assert composed.draft.query["time"]["start"] == expected
    # The previous caller cannot change a later plan without an explicit clock.
    assert _time_window("new accounts today").bounds["start"] == "2031-02-12"
    cue = _time_window("MRR end of last month", policy_context=NOW).as_of[0]
    assert cue.bounds == {"start": "2026-09-01", "end": "2026-10-01"}


def test_faithfulness_resolves_relative_windows_on_the_query_clock() -> None:
    windows = [((0, 9), {"range": {"last": {"unit": "day", "value": 1}}})]
    assert _window_agrees(windows, {"start": "2026-10-04", "end": "2026-10-05"}, NOW)
    assert not _window_agrees(windows, {"start": "2026-10-05", "end": "2026-10-06"}, NOW)


def test_a_forced_wrong_day_draft_is_held(
    subscriptions: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    result = compose(subscriptions, "new accounts today", policy_context=NOW)
    wrong = {
        **result.draft.query,
        "time": {
            **result.draft.query["time"],
            "start": "2026-10-06",
            "end": "2026-10-07",
        },
    }
    monkeypatch.setattr(
        "semantic_rails.planner.plan.compose",
        lambda *_args, **_kwargs: replace(result, draft=replace(result.draft, query=wrong)),
    )
    plan = plan_payload(
        subscriptions, intent="new accounts today", partial_query={"policy_context": NOW}
    )
    assert plan["status"] == "low_confidence"
    assert not plan["next"].get("ready_for")
    assert "time_window_unrealized" in str(plan["why"])


def test_fallback_uses_the_same_clock(
    subscriptions: Runtime, monkeypatch: pytest.MonkeyPatch, wall_clock: None
) -> None:
    from semantic_rails.planner.orchestrator import CompositionResult

    def without_primary(runtime: Runtime, intent: str) -> Any:
        return CompositionResult(parse_intent(runtime, intent), draft=None)

    monkeypatch.setattr("semantic_rails.planner.plan.compose", without_primary)
    plan = plan_payload(
        subscriptions, intent="new accounts today", partial_query={"policy_context": NOW}
    )
    query = plan["best"]["query_ir"]
    assert query["time"]["start"] == "2026-10-05"
