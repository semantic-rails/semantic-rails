"""Named date readings are bounded and verified against customer signup SQL."""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.planner.time_windows import _time_window
from semantic_rails.runtime import Runtime

NOW = {"now": "2026-10-05T06:00:00Z"}
SEED = """
CREATE TABLE events (event_id INTEGER, kind VARCHAR, segment VARCHAR, occurred_at DATE);
INSERT INTO events VALUES (1, 'signup', 'customer', '2026-09-02'),
 (2, 'signup', 'customer', '2026-09-22'), (3, 'signup', 'internal', '2026-09-29'),
 (4, 'signup', 'customer', '2026-09-30'), (5, 'close', 'customer', '2026-10-01'),
 (6, 'upgrade', 'customer', '2026-10-02');
CREATE TABLE account_day (account_id VARCHAR, day DATE, mrr DOUBLE);
INSERT INTO account_day VALUES ('a', '2026-10-04', 99), ('b', '2026-10-04', 500);
"""


@pytest.fixture(scope="module")
def subscriptions(tmp_path_factory: pytest.TempPathFactory):
    tmp_path = tmp_path_factory.mktemp("named-periods")
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
    segment: {kind: categorical, domain: [customer, internal]}
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
        all:
          - {field: dimension.subscriptions_event_kind, op: '=', value: signup}
          - {field: dimension.subscriptions_event_segment, op: '=', value: customer}
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
    runtime._get_adapter()  # the module fixture owns the connection across test teardown
    try:
        yield runtime
    finally:
        runtime.close()


ANSWER_CASES = [
    ("in September", "2026-09-01", "2026-10-01", 3),
    ("in Q3", "2026-07-01", "2026-10-01", 3),
    ("for Q3", "2026-07-01", "2026-10-01", 3),
    ("in December", "2025-12-01", "2026-01-01", 0),
    ("in the first half", "2026-01-01", "2026-07-01", 0),
    ("since September", "2026-09-01", "2026-10-05", 3),
    ("since Sept 22", "2026-09-22", "2026-10-05", 2),
    ("since Q3", "2026-07-01", "2026-10-05", 3),
    ("on Wed Sept 30", "2026-09-30", "2026-10-01", 1),
    ("on September 1st", "2026-09-01", "2026-09-02", 0),
    ("on Sept 30", "2026-09-30", "2026-10-01", 1),
    ("in September 2026", "2026-09-01", "2026-10-01", 3),
    ("in Q3 2026", "2026-07-01", "2026-10-01", 3),
    ("on September 30, 2026", "2026-09-30", "2026-10-01", 1),
    ("have we had in total", None, None, 3),
    ("ever", None, None, 3),
    ("of all time", None, None, 3),
    ("all time", None, None, 3),
    ("to date", None, None, 3),
    ("since launch", None, None, 3),
    ("since the beginning", None, None, 3),
    ("since we started", None, None, 3),
]


@pytest.mark.parametrize(("phrase", "start", "end", "expected"), ANSWER_CASES)
def test_named_reading_matches_reference_sql(subscriptions, phrase, start, end, expected):
    intent = f"How many new accounts {phrase}?"
    plan = plan_payload(subscriptions, intent=intent, partial_query={"policy_context": NOW})
    assert plan["status"] == "ok", plan.get("why")
    assert "execute" in plan["next"]["ready_for"]
    query = plan["best"]["query_ir"]
    time = query.get("time", {})
    assert time.get("start") == start and time.get("end") == end
    reference = (
        "SELECT COUNT(DISTINCT event_id) FROM events WHERE kind='signup' AND segment='customer'"
    )
    if start:
        reference += f" AND occurred_at >= DATE '{start}' AND occurred_at < DATE '{end}'"
    with duckdb.connect(subscriptions.db_path, read_only=True) as connection:
        gold = connection.execute(reference).fetchone()[0]
    assert gold == expected
    rows = subscriptions.query(query)["rows"]
    assert sum(row[query["select"][0]["as"]] or 0 for row in rows) == gold
    if start is None:
        assert "all time: no start date" in plan["assumptions"]
    elif "2026" not in phrase:
        assert any(start[:4] in assumption for assumption in plan["assumptions"])


@pytest.mark.parametrize("phrase", ["in October", "in Q4", "in the second half", "on Thu Sept 30"])
def test_ambiguous_reading_names_both_options(subscriptions, phrase):
    plan = plan_payload(
        subscriptions, intent=f"new accounts {phrase}", partial_query={"policy_context": NOW}
    )
    assert plan["status"] == "needs_clarification", plan.get("why")
    assert plan["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
    assert len(plan["why"]["details"]["possible_readings"]) == 2
    assert not plan.get("best", {}).get("query_ir")
    assert "execute" not in plan.get("next", {}).get("ready_for", [])


@pytest.mark.parametrize(
    "phrase", ["all time", "of all time", "ever", "since launch", "to date", "in total"]
)
def test_all_time_stock_stays_held(subscriptions, phrase):
    plan = plan_payload(
        subscriptions, intent=f"MRR {phrase}", partial_query={"policy_context": NOW}
    )
    assert plan["status"] != "ok"
    assert "execute" not in plan.get("next", {}).get("ready_for", [])


@pytest.mark.parametrize(
    "phrase",
    [
        "in early September",
        "in late September",
        "in mid-September",
        "September and December",
        "in September and Q3",
        "from September to December",
        "before September",
        "as of September",
        "in fiscal Q3",
        "on Sept 31",
        "on February 30",
        "since early September",
        "all time in September",
        "ever since September",
        "ytd",
        "mtd",
    ],
)
def test_unknown_or_multiple_time_readings_are_not_executable(subscriptions, phrase):
    plan = plan_payload(
        subscriptions, intent=f"new accounts {phrase}", partial_query={"policy_context": NOW}
    )
    assert plan["status"] != "ok", plan.get("best")
    assert "execute" not in plan.get("next", {}).get("ready_for", [])


@pytest.mark.parametrize("phrase", [row[0] for row in ANSWER_CASES])
def test_reading_records_the_exact_span(phrase):
    intent = f"new accounts {phrase}?"
    read = _time_window(intent, NOW)
    assert read.spans
    for span in read.spans:
        assert intent[span[0] : span[1]].lower().strip() in phrase.lower()


@pytest.mark.parametrize("phrase", ["ever", "all time", "since launch"])
def test_caller_cannot_silently_bound_all_time(subscriptions, phrase):
    plan = plan_payload(
        subscriptions,
        intent=f"new accounts {phrase}",
        partial_query={
            "policy_context": NOW,
            "time": {"start": "2026-09-22", "end": "2026-10-01"},
        },
    )
    assert plan["status"] != "ok"
    assert "execute" not in plan.get("next", {}).get("ready_for", [])


@pytest.mark.parametrize(
    ("phrase", "start", "end"),
    [
        ("December", "2025-12-01", "2026-01-01"),
        ("on Dec 31", "2025-12-31", "2026-01-01"),
        ("on February 29", "2024-02-29", "2024-03-01"),
    ],
)
def test_latest_date_is_before_the_reference(phrase, start, end):
    read = _time_window(phrase, NOW)
    assert read.bounds == {"start": start, "end": end}
    assert date.fromisoformat(start) <= date(2026, 10, 5)


@pytest.mark.parametrize("phrase", ["year to date", "month to date", "year-to-date", "ytd", "mtd"])
def test_period_to_date_is_not_read_as_all_time(phrase):
    read = _time_window(f"new accounts {phrase}", NOW)
    assert "all time: no start date" not in read.assumptions
    assert not any(not bounds for _span, bounds in read.windows)


@pytest.mark.parametrize("phrase", ["all time", "ever", "since launch"])
def test_forced_day_grain_does_not_make_an_all_time_stock_ready(subscriptions, phrase):
    plan = plan_payload(
        subscriptions,
        intent=f"MRR {phrase}",
        partial_query={
            "policy_context": NOW,
            "time": {
                "temporal_role": "temporal_role.subscriptions_account_day_day",
                "grain": "day",
            },
        },
    )
    assert plan["status"] != "ok"
    assert "execute" not in plan.get("next", {}).get("ready_for", [])


def test_generated_bounded_draft_cannot_answer_all_time(subscriptions):
    from semantic_rails.planner.time_checks import _time_window_gaps

    query = {
        "version": 1,
        "select": [
            {"as": "new_accounts", "expression": {"metric": "metric.subscriptions.new_accounts"}}
        ],
        "time": {
            "temporal_role": "temporal_role.subscriptions_event_occurred_at",
            "grain": "month",
            "start": "2026-09-01",
            "end": "2026-10-01",
        },
        "policy_context": NOW,
    }
    [gap] = _time_window_gaps(subscriptions, "new accounts in total", query)
    assert gap.kind == "time_window_unrealized"


def test_named_period_uses_the_reference_timezone():
    now = {"now": "2026-10-01T01:00:00Z"}
    assert _time_window("new accounts in September", now, timezone="UTC").bounds == {
        "start": "2026-09-01",
        "end": "2026-10-01",
    }
    local = _time_window("new accounts in September", now, timezone="America/New_York")
    assert not local.bounds and len(local.readings) == 2
