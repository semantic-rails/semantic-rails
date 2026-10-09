"""Named date readings are bounded and verified against customer signup SQL."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from shutil import copytree

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
      kind: aggregate
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
    value_type: number
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
    ("ever since September", "2026-09-01", "2026-10-05", 3),
    ("since Sept 22", "2026-09-22", "2026-10-05", 2),
    ("since Q3", "2026-07-01", "2026-10-05", 3),
    ("on Wed Sept 30", "2026-09-30", "2026-10-01", 1),
    ("on September 1st", "2026-09-01", "2026-09-02", 0),
    ("on Sept 30", "2026-09-30", "2026-10-01", 1),
    ("in Sep", "2026-09-01", "2026-10-01", 3),
    ("for the third quarter", "2026-07-01", "2026-10-01", 3),
    ("in H1", "2026-01-01", "2026-07-01", 0),
    ("since September 1st", "2026-09-01", "2026-10-05", 3),
    ("since October", "2026-10-01", "2026-10-05", 0),
    ("since Q4", "2026-10-01", "2026-10-05", 0),
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


@pytest.mark.parametrize(
    ("phrase", "now"),
    [
        ("on October 5", NOW),
        ("on February 29", {"now": "2024-02-29T06:00:00Z"}),
    ],
)
def test_current_named_day_clarification_has_no_reversed_range(phrase, now):
    read = _time_window(f"new accounts {phrase}", now)
    assert not read.bounds and len(read.readings) == 2
    assert "not a complete day yet" in read.readings[0]
    assert "to" in read.readings[1]


SHOP_NOW = {"now": "2024-07-05T06:00:00Z"}


@pytest.fixture(scope="module")
def shop(tmp_path_factory):
    source = Path(__file__).resolve().parents[1] / "integration/correctness/shop"
    package = tmp_path_factory.mktemp("named-shop") / "shop"
    copytree(source, package)
    runtime = Runtime.from_path(str(package))
    runtime._get_adapter()  # the module fixture owns its connection
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("phrase", "start", "end", "expected"),
    [
        ("not in June", "2024-06-01", "2024-07-01", 2),
        ("excluding Q2", "2024-04-01", "2024-07-01", 2),
        ("except in December", "2023-12-01", "2024-01-01", 3),
        # Dotted abbreviations are one time phrase, wholly inside the exclusion.
        ("not on Jun. 25", "2024-06-25", "2024-06-26", 2),
        ("excluding Jun. 25, 2024", "2024-06-25", "2024-06-26", 2),
        ("not on Tue. June 25", "2024-06-25", "2024-06-26", 2),
        ("excluding Tue. June 25", "2024-06-25", "2024-06-26", 2),
        # The current day offers readings unless an exclusion names it.
        ("not on Fri July 5", "2024-07-05", "2024-07-06", 3),
    ],
)
def test_excluded_period_never_becomes_a_positive_window(shop, phrase, start, end, expected):
    reference = (
        "SELECT COUNT(DISTINCT customer_id) FROM signups WHERE channel <> 'store' "
        f"AND (signed_up_at < TIMESTAMP '{start}' OR signed_up_at >= TIMESTAMP '{end}')"
    )
    with duckdb.connect(shop.db_path, read_only=True) as connection:
        assert connection.execute(reference).fetchone()[0] == expected
    intent = f"signups {phrase}"
    plan = plan_payload(
        shop,
        intent=intent,
        partial_query={
            "policy_context": SHOP_NOW,
            "where": [{"field": "dimension.shop_customer_channel", "op": "!=", "value": "store"}],
        },
    )
    assert plan["status"] == "low_confidence", plan
    assert plan["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    gaps = {gap["kind"]: gap for gap in plan["why"]["details"]["gaps"]}
    assert [item["kind"] for item in gaps["negation_unrealized"]["expected"]["items"]] == ["time"]
    assert "time_window_unresolved" in gaps
    assert "execute" not in plan["next"].get("ready_for", [])
    read = _time_window(intent, SHOP_NOW)
    assert read.unresolved and not read.windows and not read.bounds
    assert read.excluded and not read.readings


@pytest.mark.parametrize(
    "phrase", ["not in 2024", "not last month", "not all time", "all periods but in June"]
)
def test_exclusion_guard_covers_every_window_parser(phrase):
    read = _time_window(f"signups {phrase}", SHOP_NOW)
    assert read.unresolved and not read.windows and not read.bounds
    assert "all time: no start date" not in read.assumptions


def test_a_window_after_an_excluded_value_stays_positive():
    read = _time_window("signups excluding store last month", SHOP_NOW)
    assert read.bounds == {"range": {"last": {"unit": "month", "value": 1}}}
    assert not read.excluded and not read.unresolved


@pytest.mark.parametrize(
    "phrase", ["since the beginning of the year", "since launch of the new plan"]
)
def test_scoped_beginning_is_not_all_time(shop, phrase):
    plan = plan_payload(
        shop, intent=f"signups {phrase}", partial_query={"policy_context": SHOP_NOW}
    )
    assert plan["status"] != "ok", plan
    assert "execute" not in plan["next"].get("ready_for", [])
    assert "all time: no start date" not in plan.get("assumptions", [])
    read = _time_window(f"signups {phrase}", SHOP_NOW)
    assert not any(not bounds for _span, bounds in read.windows)


@pytest.mark.parametrize(
    ("intent", "table", "key", "clock", "start", "end", "expected"),
    [
        (
            "signups in total last month",
            "signups",
            "DISTINCT customer_id",
            "signed_up_at",
            "2024-06-01",
            "2024-07-01",
            2,
        ),
        (
            "signups ever last month",
            "signups",
            "DISTINCT customer_id",
            "signed_up_at",
            "2024-06-01",
            "2024-07-01",
            2,
        ),
        (
            "signups in  total last month",
            "signups",
            "DISTINCT customer_id",
            "signed_up_at",
            "2024-06-01",
            "2024-07-01",
            2,
        ),
        (
            "orders in total in Q1 2024",
            "orders",
            "order_id",
            "ordered_at",
            "2024-01-01",
            "2024-04-01",
            3,
        ),
        (
            "signups March 2",
            "signups",
            "DISTINCT customer_id",
            "signed_up_at",
            "2024-03-02",
            "2024-03-03",
            1,
        ),
        (
            "signups Mon May 6",
            "signups",
            "DISTINCT customer_id",
            "signed_up_at",
            "2024-05-06",
            "2024-05-07",
            1,
        ),
    ],
)
def test_bounded_reading_matches_shop_reference_sql(
    shop, intent, table, key, clock, start, end, expected
):
    plan = plan_payload(shop, intent=intent, partial_query={"policy_context": SHOP_NOW})
    assert plan["status"] == "ok", plan.get("why")
    assert "execute" in plan["next"]["ready_for"]
    query = plan["best"]["query_ir"]
    read = _time_window(intent, SHOP_NOW)
    from semantic_rails.ast import _relative_range_bounds

    bounds = read.bounds
    if "range" in bounds:
        bounds = _relative_range_bounds(bounds["range"], policy_context=SHOP_NOW)
    assert bounds == {"start": start, "end": end}
    time = query["time"]
    carried = (
        _relative_range_bounds(time["range"], policy_context=SHOP_NOW)
        if "range" in time
        else {"start": time["start"], "end": time["end"]}
    )
    assert carried == bounds
    assert "all time: no start date" not in plan.get("assumptions", [])
    reference = (
        f"SELECT COUNT({key}) FROM {table} "
        f"WHERE {clock} >= TIMESTAMP '{start}' AND {clock} < TIMESTAMP '{end}'"
    )
    with duckdb.connect(shop.db_path, read_only=True) as connection:
        gold = connection.execute(reference).fetchone()[0]
    assert gold == expected
    rows = shop.query(query)["rows"]
    # One bounded window is one row: an empty result can't pass as a zero.
    assert [row[query["select"][0]["as"]] for row in rows] == [gold]
    for word in ("ever", "in total", "in  total"):
        if word in intent:
            assert any(intent[start:end] == word for start, end in read.spans)


@pytest.mark.parametrize(
    "phrase", ["all time", "since launch", "to date", "since the beginning", "since we started"]
)
def test_explicit_all_time_still_conflicts_with_a_bounded_window(shop, phrase):
    plan = plan_payload(
        shop, intent=f"signups {phrase} in September", partial_query={"policy_context": SHOP_NOW}
    )
    assert plan["status"] != "ok", plan
    assert "execute" not in plan["next"].get("ready_for", [])


@pytest.mark.parametrize(
    "intent",
    [
        "revenue for customer April",
        "Jan's revenue",
        "signups from the June promotion",
        "signups first half hour",
        "signups first quarter hour",
        *[
            f"signups from the {month} promotion"
            for month in (
                "January",
                "February",
                "March",
                "April",
                "May",
                "June",
                "July",
                "August",
                "September",
                "October",
                "November",
                "December",
            )
        ],
    ],
)
def test_non_time_month_or_fraction_does_not_resolve_a_window(intent):
    read = _time_window(intent, SHOP_NOW)
    assert not read.windows and not read.bounds


def test_unscoped_day_still_checks_its_weekday(shop):
    plan = plan_payload(
        shop, intent="signups Tue May 6", partial_query={"policy_context": SHOP_NOW}
    )
    assert plan["status"] == "needs_clarification", plan
    assert "execute" not in plan["next"].get("ready_for", [])
