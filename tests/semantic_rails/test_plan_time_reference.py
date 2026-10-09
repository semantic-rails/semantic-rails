"""Planning uses the caller's clock and holds unconsumed as-of cues."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.planner import plan_payload
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.orchestrator import compose
from semantic_rails.planner.time_checks import _window_agrees
from semantic_rails.planner.time_windows import _time_window
from semantic_rails.query_call import read_call
from semantic_rails.runtime import Runtime

NOW = {"now": "2026-10-05T06:00:00Z"}
SEED = """
CREATE TABLE events (event_id INTEGER, kind VARCHAR, occurred_at DATE);
INSERT INTO events VALUES (1, 'signup', '2026-10-05'), (2, 'signup', '2026-10-05'),
  (3, 'signup', '2026-10-06'), (4, 'close', '2026-10-05');
CREATE TABLE account_day (account_id VARCHAR, day DATE, mrr DOUBLE);
INSERT INTO account_day VALUES ('a', '2026-10-04', 99), ('b', '2026-10-04', 500);
"""


def _alias(item: Any) -> str:
    """A select item's output column: an id's last segment, or its ``as``."""
    return item.split(".")[-1] if isinstance(item, str) else item["as"]


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
        all: [{field: dimension.subscriptions_event_kind, op: '=', value: signup}]
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
    try:
        yield runtime
    finally:
        runtime.close()


def _set_wall_clock(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    # The server clock is read in one place.
    monkeypatch.setattr("semantic_rails.clock.current_time", lambda: moment)


@pytest.fixture()
def wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_wall_clock(monkeypatch, datetime(2031, 2, 12, 1, tzinfo=UTC))


@pytest.fixture()
def local_subscriptions(subscriptions: Runtime) -> Runtime:
    subscriptions._config = replace(
        subscriptions._config,
        temporal_roles=[
            replace(role, timezone="America/New_York")
            for role in subscriptions._config.temporal_roles
        ],
    )
    with duckdb.connect(subscriptions.db_path) as connection:
        connection.execute("UPDATE events SET occurred_at = occurred_at - INTERVAL 1 DAY")
    return subscriptions


@pytest.fixture()
def local_orders(runtime_factory) -> Runtime:
    runtime = runtime_factory("jaffle_shop")
    runtime._config = replace(
        runtime._config,
        temporal_roles=[
            replace(role, timezone="America/New_York") for role in runtime._config.temporal_roles
        ],
    )
    return runtime


def _with_package_zone(runtime: Runtime, timezone: str) -> None:
    snapshot = runtime._snapshot
    runtime._snapshot = replace(
        snapshot, _normalized={**snapshot.normalized, "defaults": {"time": {"timezone": timezone}}}
    )


def _signups(runtime: Runtime, start: str, end: str) -> int:
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        return connection.execute(
            "SELECT COUNT(DISTINCT event_id) FROM events WHERE kind = 'signup' "
            "AND occurred_at >= CAST(? AS DATE) AND occurred_at < CAST(? AS DATE)",
            [start, end],
        ).fetchone()[0]


def _assert_window_gap(plan: dict[str, Any]) -> None:
    """Held: the call's window is not the one the question states, so it isn't executable."""
    assert plan["status"] == "low_confidence", plan.get("why")
    assert plan["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP", plan.get("why")
    assert "time_window_unrealized" in [gap["kind"] for gap in plan["why"]["details"]["gaps"]]
    assert "execute" not in (plan.get("next") or {}).get("ready_for", [])


def _reference(runtime: Runtime, sql: str) -> Any:
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        return connection.execute(sql).fetchone()[0]


def _answer(runtime: Runtime, plan: dict[str, Any], now: str) -> Any:
    """The one value the plan's call answers, run on the clock it was planned on."""
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query"]
    [row] = runtime.query(query, now=now)["rows"]
    return row[_alias(query["select"][0])]


# Signups on the day ``now`` falls on in a zone (DuckDB reads the zone, not the planner).
SIGNUPS_ON_LOCAL_DAY = (
    "SELECT COUNT(DISTINCT event_id) FROM events WHERE kind = 'signup' "
    "AND occurred_at = CAST(timezone('{zone}', TIMESTAMPTZ '{now}') AS DATE)"
)


def _assert_zone_hold(plan: dict[str, Any], phrases: list[str]) -> None:
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "TIME_WINDOW_UNRESOLVED", plan.get("why")
    assert plan["why"]["details"]["unresolved_phrases"] == phrases
    assert not plan.get("next", {}).get("ready_for")
    assert not (plan.get("best") or {}).get("query")
    assert not any(
        row.get("query") for row in plan.get("alternatives", []) + plan.get("blocked", [])
    )


@pytest.mark.parametrize(
    ("fixture", "intent", "now", "reference", "expected"),
    [
        # At 01:00Z New York (the role's zone) reads October 4: today is that day so far.
        (
            "local_subscriptions",
            "new accounts today",
            "2026-10-05T01:00:00Z",
            SIGNUPS_ON_LOCAL_DAY.format(zone="America/New_York", now="2026-10-05 01:00:00+00"),
            2,
        ),
        # The call's clock is the sessions role (UTC), whose yesterday is October 4; each leg
        # reads that day in its own zone, orders on New York's October 4.
        (
            "shop",
            "purchase rate yesterday",
            "2026-10-05T01:00:00Z",
            "SELECT (SELECT COUNT(DISTINCT order_id) FROM orders "
            "WHERE CAST(timezone('America/New_York', timezone('UTC', ordered_at)) AS DATE) "
            "= DATE '2026-10-04') / (SELECT COUNT(DISTINCT session_id) FROM sessions "
            "WHERE started_at >= TIMESTAMP '2026-10-04' AND started_at < TIMESTAMP '2026-10-05')",
            0.25,
        ),
        # Today so far on the sessions clock: from midnight UTC to 01:00.
        (
            "shop",
            "session count today",
            "2026-10-05T01:00:00Z",
            "SELECT COUNT(DISTINCT session_id) FROM sessions "
            "WHERE started_at >= TIMESTAMP '2026-10-05' "
            "AND started_at < TIMESTAMP '2026-10-05 01:00:00'",
            2,
        ),
    ],
    ids=["local-today", "mixed-zone-ratio-yesterday", "utc-today-so-far"],
)
def test_a_relative_window_is_read_in_the_roles_zone_at_the_requests_now(
    request: pytest.FixtureRequest,
    fixture: str,
    intent: str,
    now: str,
    reference: str,
    expected: Any,
) -> None:
    runtime = request.getfixturevalue(fixture)
    plan = plan_payload(runtime, intent=intent, partial_query={}, now=now)
    assert plan["status"] == "ok", plan.get("why")
    time = plan["best"]["query"]["time"]
    assert {"last", "this"} & set(time) and not {"start", "end"} & set(time)
    gold = _reference(runtime, reference)
    assert gold == expected
    assert _answer(runtime, plan, now) == gold


def test_today_executes_where_both_zones_read_the_same_day(local_subscriptions: Runtime) -> None:
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={},
        now="2026-10-05T12:00:00Z",
    )
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query"]
    assert query["time"]["this"] == "day"
    rows = local_subscriptions.query(query, now="2026-10-05T12:00:00Z")["rows"]
    gold = _signups(local_subscriptions, "2026-10-05", "2026-10-06")
    assert gold == 1
    assert sum(row[_alias(query["select"][0])] for row in rows) == gold


@pytest.mark.parametrize(
    ("phrase", "now"),
    [
        ("today", "2026-10-05T01:00:00Z"),
        ("yesterday", "2026-10-05T01:00:00Z"),
        ("this week", "2026-10-05T01:00:00Z"),
        ("this month", "2026-11-01T02:00:00Z"),
        ("this quarter", "2026-10-01T02:00:00Z"),
        ("this year", "2027-01-01T02:00:00Z"),
    ],
)
def test_orders_windows_are_answered_where_the_roles_zone_reads_other_days(
    local_orders: Runtime, phrase: str, now: str
) -> None:
    plan = plan_payload(
        local_orders,
        intent=f"orders {phrase}",
        partial_query={},
        now=now,
    )
    assert plan["status"] == "ok", plan.get("why")
    assert plan["next"]["ready_for"] == ["execute"]
    assert {"last", "this"} & set(plan["best"]["query"]["time"])


@pytest.mark.parametrize(
    ("phrase", "now", "start", "end"),
    [
        # 12:00Z is 08:00 on October 5 in New York: today runs from its midnight to now.
        ("today", "2026-10-05T12:00:00Z", "2026-10-05", "2026-10-05T08:00:00"),
        ("yesterday", "2026-10-05T12:00:00Z", "2026-10-04", "2026-10-05"),
        # A now without an offset is a UTC instant: 21:00 on October 4 in New York.
        ("today", "2026-10-05T01:00:00", "2026-10-04", "2026-10-04T21:00:00"),
        ("yesterday", "2026-10-05T01:00:00", "2026-10-03", "2026-10-04"),
        # A date is its first instant in UTC: 20:00 on October 4 in New York.
        ("today", "2026-10-05", "2026-10-04", "2026-10-04T20:00:00"),
    ],
)
def test_orders_windows_are_read_on_the_roles_clock_at_now(
    local_orders: Runtime, phrase: str, now: str, start: str, end: str
) -> None:
    plan = plan_payload(
        local_orders,
        intent=f"orders {phrase}",
        partial_query={},
        now=now,
    )
    assert plan["status"] == "ok", plan.get("why")
    # Reference first: the role's wall clock at now, read by DuckDB.
    with duckdb.connect(":memory:") as connection:
        connection.execute("SET TimeZone = 'UTC'")
        [(local,)] = connection.execute(
            "SELECT timezone('America/New_York', CAST(? AS TIMESTAMPTZ))", [now]
        ).fetchall()
    day = local.date()
    reference = (
        (day.isoformat(), local.isoformat())
        if phrase == "today"
        else ((day - timedelta(days=1)).isoformat(), day.isoformat())
    )
    assert reference == (start, end)
    # The returned call reads that window on the clock it was planned on.
    time = read_call(plan["best"]["query"], config=local_orders._config, now=now)[0]["time"]
    assert (time["start"], time["end"]) == (start, end)


def test_the_wall_clock_reads_today_in_the_roles_zone(
    local_subscriptions: Runtime, wall_clock: None
) -> None:
    plan = plan_payload(local_subscriptions, intent="new accounts today")
    assert plan["status"] == "ok", plan.get("why")
    assert plan["best"]["query"]["time"]["this"] == "day"


def test_a_period_comparison_is_held_where_the_roles_zone_reads_another_year(
    local_orders: Runtime,
) -> None:
    plan = plan_payload(
        local_orders,
        intent="monthly revenue this year compared to last year",
        partial_query={},
        now="2027-01-01T02:00:00Z",
    )
    # New York still reads 2026, a year not over yet: compared with a complete one, it's held.
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "PERIOD_COMPARISON_INCOMPLETE", plan.get("why")
    assert plan["why"]["details"]["window"] == {"this": "year"}
    assert "execute" not in (plan.get("next") or {}).get("ready_for", [])


def test_a_period_comparison_with_a_year_not_over_is_held(local_orders: Runtime) -> None:
    plan = plan_payload(
        local_orders,
        intent="monthly revenue this year compared to last year",
        partial_query={},
        now="2026-07-01T12:00:00Z",
    )
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "PERIOD_COMPARISON_INCOMPLETE", plan.get("why")
    assert plan["why"]["details"]["window"] == {"this": "year"}
    assert "execute" not in (plan.get("next") or {}).get("ready_for", [])


@pytest.mark.parametrize("caller_window", [False, True])
def test_a_package_zone_reading_answers_on_the_roles_day(
    local_subscriptions: Runtime, caller_window: bool
) -> None:
    # Tokyo reads 2026-10-06; New York (the role's zone) reads 2026-10-05, the caller's day.
    _with_package_zone(local_subscriptions, "Asia/Tokyo")
    window = {"time": {"start": "2026-10-05", "end": "2026-10-06"}} if caller_window else {}
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={"policy_context": {"now": "2026-10-05T16:00:00Z"}, **window},
    )
    gold = _reference(
        local_subscriptions,
        SIGNUPS_ON_LOCAL_DAY.format(zone="America/New_York", now="2026-10-05 16:00:00+00"),
    )
    assert gold == 1
    assert _answer(local_subscriptions, plan, "2026-10-05T16:00:00Z") == gold


@pytest.mark.parametrize(
    ("start", "end", "answered"),
    [("2026-10-06", "2026-10-07", True), ("2026-10-05", "2026-10-06", False)],
    ids=["the-roles-day", "the-utc-day"],
)
def test_a_caller_window_must_be_the_roles_day(
    subscriptions: Runtime, start: str, end: str, answered: bool
) -> None:
    # Tokyo is both the role's and the planning zone and reads 2026-10-06; UTC reads
    # 2026-10-05. Only the role's day is today; the UTC one is another local day.
    subscriptions._config = replace(
        subscriptions._config,
        temporal_roles=[
            replace(role, timezone="Asia/Tokyo") for role in subscriptions._config.temporal_roles
        ],
    )
    _with_package_zone(subscriptions, "Asia/Tokyo")
    plan = plan_payload(
        subscriptions,
        intent="new accounts today",
        partial_query={
            "time": {"start": start, "end": end},
        },
        now="2026-10-05T16:00:00Z",
    )
    if not answered:
        _assert_window_gap(plan)
        return
    gold = _reference(
        subscriptions, SIGNUPS_ON_LOCAL_DAY.format(zone="Asia/Tokyo", now="2026-10-05 16:00:00+00")
    )
    assert gold == 1
    assert _answer(subscriptions, plan, "2026-10-05T16:00:00Z") == gold


def test_a_callers_range_is_held_where_the_roles_zone_reads_other_days(
    subscriptions: Runtime,
) -> None:
    # "last month" reads September in Tokyo; the role's last 30 days end on October 2 there, so
    # they are not last month.
    subscriptions._config = replace(
        subscriptions._config,
        temporal_roles=[
            replace(role, timezone="Asia/Tokyo") for role in subscriptions._config.temporal_roles
        ],
    )
    plan = plan_payload(
        subscriptions,
        intent="monthly new accounts last month",
        partial_query={
            "time": {"last": {"unit": "day", "value": 30}},
        },
        now="2026-10-01T16:00:00Z",
    )
    _assert_window_gap(plan)


SHOP_NOW = datetime(2026, 10, 5, 1, tzinfo=UTC)  # 21:00 on October 4 in New York
SHOP_SESSIONS = "temporal_role.shop_session_started_at"
SHOP_ORDERS = "temporal_role.shop_order_ordered_at"


@pytest.fixture()
def shop(tmp_path: Path):
    """A ratio whose denominator is read on a UTC role and its numerator on a New York one."""

    files = {
        "package.yml": """
schema_version: 1
package:
  id: shop
  namespace: shop
  name: Shop
  description: Storefront sessions and orders
  warehouse: duckdb
  default_db: shop.duckdb
  seed: {kind: external}
defaults:
  time: {timezone: UTC}
""",
        "graph.yml": """
graph:
  entities:
    session: {key: [session_id], model: sessions}
    order: {key: [order_id], model: orders}
""",
        "models/sessions.yml": """
model:
  id: sessions
  relation: sessions
  entities: {session: {}}
  times:
    started_at: {column: started_at, kind: timestamp, class: event_time, default: true}
  measures:
    sessions_all: {kind: entity_count, entity_key: session_id, value_type: count, publish: false}
""",
        "models/orders.yml": """
model:
  id: orders
  relation: orders
  entities: {order: {}}
  times:
    ordered_at: {column: ordered_at, kind: timestamp, class: event_time, default: true,
      column_timezone: UTC, timezone: America/New_York}
  measures:
    orders_all: {kind: entity_count, entity_key: order_id, value_type: count, publish: false}
""",
        "metrics/shop.yml": f"""
metrics:
  purchase_rate:
    label: Purchase rate
    description: Orders per storefront session.
    kind: ratio
    value_type: number
    numerator: orders_all
    denominator: sessions_all
    temporal_role: {SHOP_SESSIONS}
  session_count:
    label: Session count
    description: Storefront sessions.
    kind: aggregate
    value_type: count
    temporal_role: {SHOP_SESSIONS}
    expression:
      kind: aggregate
      measure: measure.shop.sessions_all
      aggregation: count_distinct
""",
    }
    for name, contents in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    with duckdb.connect(str(tmp_path / "shop.duckdb")) as connection:
        connection.execute(
            """
CREATE TABLE sessions (session_id INTEGER, started_at TIMESTAMP);
INSERT INTO sessions VALUES (1, '2026-10-04 12:00'), (2, '2026-10-04 12:00'),
  (3, '2026-10-04 12:00'), (4, '2026-10-04 12:00'), (5, '2026-10-05 00:10'),
  (6, '2026-10-05 00:10');
CREATE TABLE orders (order_id INTEGER, ordered_at TIMESTAMP);
INSERT INTO orders VALUES (1, '2026-10-04 02:00'), (2, '2026-10-04 02:00'),
  (3, '2026-10-05 00:30');
"""
        )
    runtime = Runtime.from_path(str(tmp_path))
    try:
        yield runtime
    finally:
        runtime.close()


def _plan_on(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, surface: str, intent: str, **kwargs: Any
) -> dict[str, Any]:
    """Plan through ``plan_payload`` with ``policy_context.now`` or through the MCP handler on
    the same wall clock."""

    if surface == "mcp":
        _set_wall_clock(monkeypatch, SHOP_NOW)
        return SemanticLayerMCPAdapter(runtime).call_tool("plan", {"intent": intent, **kwargs})
    context = {"now": SHOP_NOW.isoformat()}
    return plan_payload(runtime, intent=intent, partial_query={"policy_context": context}, **kwargs)


def _shop_value(runtime: Runtime, plan: dict[str, Any], now: Any) -> Any:
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query"]
    [row] = runtime.query(query, now=now)["rows"]
    return row[_alias(query["select"][0])]


def _shop_reference(runtime: Runtime, sql: str) -> Any:
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        return connection.execute(sql).fetchone()[0]


@pytest.mark.parametrize("surface", ["python", "mcp"])
@pytest.mark.parametrize("detail", ["best", "full", "query", "debug"])
@pytest.mark.parametrize("phrase", ["yesterday", "today"])
def test_a_window_a_leg_reads_in_its_own_zone_is_answered(
    shop: Runtime, monkeypatch: pytest.MonkeyPatch, surface: str, detail: str, phrase: str
) -> None:
    # Sessions are read in UTC, orders on New York's day. The call's window is read on its own
    # clock (the sessions role), and each leg reads those days in its zone: answered, not held.
    plan = _plan_on(shop, monkeypatch, surface, f"purchase rate {phrase}", detail=detail)
    assert plan["status"] == "ok", plan.get("why")
    time = plan["best"]["query"]["time"]
    assert time["temporal_role"] == SHOP_SESSIONS
    assert {"last", "this"} & set(time) and not {"start", "end"} & set(time)


def test_a_window_every_leg_reads_on_the_same_days_executes(shop: Runtime) -> None:
    # At 12:00Z UTC and New York both read October 4 as yesterday.
    plan = plan_payload(
        shop,
        intent="purchase rate yesterday",
        partial_query={},
        now="2026-10-05T12:00:00Z",
    )
    # Each leg is read on its own local day.
    reference = _shop_reference(
        shop,
        "SELECT (SELECT COUNT(DISTINCT order_id) FROM orders "
        "WHERE CAST(timezone('America/New_York', timezone('UTC', ordered_at)) AS DATE) "
        "= DATE '2026-10-04') / (SELECT COUNT(DISTINCT session_id) FROM sessions "
        "WHERE started_at >= TIMESTAMP '2026-10-04' AND started_at < TIMESTAMP '2026-10-05')",
    )
    assert reference == 0.25
    assert _shop_value(shop, plan, "2026-10-05T12:00:00Z") == reference


def test_an_absolute_window_on_a_mixed_zone_metric_is_unchanged(shop: Runtime) -> None:
    plan = plan_payload(
        shop,
        intent="purchase rate on 4 October 2026",
        partial_query={},
        now=SHOP_NOW.isoformat(),
    )
    assert _shop_value(shop, plan, SHOP_NOW) == 0.25


def test_a_metric_whose_legs_share_the_planning_zone_is_unchanged(shop: Runtime) -> None:
    plan = plan_payload(
        shop,
        intent="session count yesterday",
        partial_query={},
        now=SHOP_NOW.isoformat(),
    )
    reference = _shop_reference(
        shop,
        "SELECT COUNT(DISTINCT session_id) FROM sessions "
        "WHERE started_at >= TIMESTAMP '2026-10-04' AND started_at < TIMESTAMP '2026-10-05'",
    )
    assert reference == 4
    assert _shop_value(shop, plan, SHOP_NOW) == reference


def test_roles_that_cannot_be_computed_are_held(
    shop: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unbound(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("binding failed")

    monkeypatch.setattr("semantic_rails.planner.time_checks.bind_query", unbound)
    # A relative window is read on the call's clock, so no role's zone holds it.
    relative = plan_payload(
        shop,
        intent="session count yesterday",
        partial_query={},
        now=SHOP_NOW.isoformat(),
    )
    assert relative["status"] == "ok", relative.get("why")
    # A named day is the latest one already begun: UTC reads 2026's October 5, New York 2025's.
    # With no roles computed, the New York orders role may read it, so it's held.
    plan = plan_payload(
        shop,
        intent="session count on October 5",
        partial_query={},
        now=SHOP_NOW.isoformat(),
    )
    _assert_zone_hold(plan, ["on october 5"])


def test_before_role_selection_uses_the_package_default_zone(
    local_subscriptions: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from semantic_rails.planner import time_windows

    snapshot = local_subscriptions._snapshot
    local_subscriptions._snapshot = replace(
        snapshot,
        _normalized={**snapshot.normalized, "defaults": {"time": {"timezone": "America/New_York"}}},
    )
    dates = []
    resolve = time_windows._resolved_time_window

    def record_date(text: str, today: date):
        dates.append(today)
        return resolve(text, today)

    monkeypatch.setattr(time_windows, "_resolved_time_window", record_date)
    parsed = parse_intent(
        local_subscriptions, "new accounts today", policy_context={"now": "2026-10-05T01:00:00Z"}
    )
    assert parsed.time["this"] == "day"
    assert set(dates) == {date(2026, 10, 4)}


def test_the_cache_tracks_local_midnight_within_one_utc_day() -> None:
    # "today" carries no date; the day an as-of cue reads it on is the local one.
    for now, expected in [
        ("2026-10-05T03:59:00Z", "2026-10-04"),
        ("2026-10-05T04:01:00Z", "2026-10-05"),
    ]:
        local = _time_window("as of today", {"now": now}, timezone="America/New_York")
        assert local.as_of[0].bounds["start"] == expected
        utc = _time_window("as of today", {"now": now}, timezone="UTC")
        assert utc.as_of[0].bounds["start"] == "2026-10-05"


def test_relative_and_absolute_windows_agree_in_the_roles_zone() -> None:
    windows = [((0, 9), {"last": {"unit": "day", "value": 1}})]
    context = {"now": "2026-10-05T01:00:00Z"}
    assert _window_agrees(
        windows, {"start": "2026-10-03", "end": "2026-10-04"}, context, timezone="America/New_York"
    )
    assert not _window_agrees(
        windows, {"start": "2026-10-04", "end": "2026-10-05"}, context, timezone="America/New_York"
    )


@pytest.mark.parametrize("caller_window", [False, True])
@pytest.mark.parametrize("detail", ["best", "full", "query", "debug"])
@pytest.mark.parametrize(
    ("phrase", "start", "end"),
    [("today", "2026-10-05", "2026-10-06"), ("yesterday", "2026-10-04", "2026-10-05")],
)
def test_a_window_resolved_in_utc_is_held_for_a_local_role(
    local_subscriptions: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    caller_window: bool,
    detail: str,
    phrase: str,
    start: str,
    end: str,
) -> None:
    context = {"now": "2026-10-05T01:00:00Z"}
    result = compose(local_subscriptions, f"new accounts {phrase}", policy_context=context)
    wrong_time = result.draft.query.time.with_window({"start": start, "end": end})
    wrong = replace(result.draft.query, time=wrong_time)
    monkeypatch.setattr(
        "semantic_rails.planner.plan.compose",
        lambda *_args, **_kwargs: replace(result, draft=replace(result.draft, query=wrong)),
    )
    partial = {
        "policy_context": context,
        **({"time": wrong_time.to_call()} if caller_window else {}),
    }
    plan = plan_payload(
        local_subscriptions, intent=f"new accounts {phrase}", partial_query=partial, detail=detail
    )
    # New York reads the phrase a day earlier: the UTC dates are another window, so held.
    _assert_window_gap(plan)
    assert plan["best"]["query"]


def test_an_explicit_window_on_the_roles_day_is_answered(local_subscriptions: Runtime) -> None:
    # At 01:00Z New York (the role's zone) reads October 4: the caller's window is that day.
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={
            "time": {"start": "2026-10-04", "end": "2026-10-05"},
        },
        now="2026-10-05T01:00:00Z",
    )
    gold = _reference(
        local_subscriptions,
        SIGNUPS_ON_LOCAL_DAY.format(zone="America/New_York", now="2026-10-05 01:00:00+00"),
    )
    assert gold == 2
    assert _answer(local_subscriptions, plan, "2026-10-05T01:00:00Z") == gold


def test_local_fallback_uses_the_same_clock(
    local_subscriptions: Runtime, monkeypatch: pytest.MonkeyPatch, wall_clock: None
) -> None:
    from semantic_rails.planner.orchestrator import CompositionResult

    def without_primary(runtime: Runtime, intent: str) -> Any:
        return CompositionResult(parse_intent(runtime, intent), draft=None)

    monkeypatch.setattr("semantic_rails.planner.plan.compose", without_primary)
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={},
        now="2026-10-05T12:00:00Z",
    )
    assert plan["status"] == "ok", plan.get("why")
    assert plan["best"]["query"]["time"]["this"] == "day"


@pytest.mark.parametrize("detail", ["best", "full", "query", "debug"])
def test_a_returned_relative_window_keeps_the_supplied_clock(
    local_subscriptions: Runtime, monkeypatch: pytest.MonkeyPatch, detail: str
) -> None:
    _set_wall_clock(monkeypatch, datetime(2026, 10, 7, 12, tzinfo=UTC))
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts yesterday",
        partial_query={},
        detail=detail,
        now="2026-10-05T12:00:00Z",
    )
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query"]
    assert query["time"]["last"] == {"unit": "day", "value": 1}
    assert "policy_context" not in query
    # Run unchanged, on the clock it was planned on.
    rows = local_subscriptions.query(query, now="2026-10-05T12:00:00Z")["rows"]
    with duckdb.connect(local_subscriptions.db_path, read_only=True) as connection:
        gold = connection.execute(
            "SELECT COUNT(DISTINCT event_id) FROM events WHERE kind='signup' "
            "AND occurred_at >= DATE '2026-10-04' AND occurred_at < DATE '2026-10-05'"
        ).fetchone()[0]
    assert gold == 2
    assert sum(row[_alias(query["select"][0])] for row in rows) == gold
    # No returned call carries dates for the relative phrase.
    assert not any(
        {"start", "end"} & set((row.get("query") or {}).get("time", {}))
        for row in plan.get("alternatives", []) + plan.get("blocked", [])
    )


@pytest.mark.parametrize("detail", ["best", "full", "query", "debug"])
def test_offset_bounds_on_another_local_day_are_held(
    local_subscriptions: Runtime, detail: str
) -> None:
    # Both bounds fall on 2026-10-03 in New York.
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={
            "time": {"start": "2026-10-04T01:00:00Z", "end": "2026-10-04T02:00:00Z"},
        },
        detail=detail,
        now="2026-10-04T12:00:00Z",
    )
    assert plan["status"] == "low_confidence", plan.get("why")
    assert plan["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert not plan.get("next", {}).get("ready_for")


@pytest.mark.parametrize("detail", ["best", "full", "query", "debug"])
@pytest.mark.parametrize(
    ("start", "end"),
    [
        # Reversed or empty: the window holds no row.
        ("2026-10-04T12:00:00-04:00", "2026-10-04T01:00:00-04:00"),
        ("2026-10-04T12:00:00-04:00", "2026-10-04T12:00:00-04:00"),
        ("2026-10-05", "2026-10-04"),
        # Part of the day: on a timestamp clock it leaves out the rest of the day.
        ("2026-10-04T12:00:00-04:00", "2026-10-04T23:59:59-04:00"),
        ("2026-10-04T00:00:00-04:00", "2026-10-04T23:59:59-04:00"),
        # Midnight in an offset other than UTC's.
        ("2026-10-04T00:00:00-04:00", "2026-10-05T00:00:00-04:00"),
    ],
)
def test_a_caller_window_is_read_only_at_whole_days(
    local_subscriptions: Runtime, start: str, end: str, detail: str
) -> None:
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={
            "time": {"start": start, "end": end},
        },
        detail=detail,
        now="2026-10-04T12:00:00Z",
    )
    assert plan["status"] == "low_confidence", plan.get("why")
    assert plan["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert [gap["kind"] for gap in plan["why"]["details"]["gaps"]] == ["time_window_unrealized"]
    assert not plan.get("next", {}).get("ready_for")


@pytest.mark.parametrize("detail", ["best", "full", "query", "debug"])
@pytest.mark.parametrize(
    "time",
    [
        # Without its end the window also reads October 5.
        {"start": "2026-10-04", "end": None},
        {"grain": "day", "start": "2026-10-04", "end": None},
        # Without its start the window also reads October 3.
        {"start": None, "end": "2026-10-05"},
    ],
)
def test_a_caller_window_that_clears_a_bound_is_held(
    local_subscriptions: Runtime, time: dict[str, Any], detail: str
) -> None:
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={"time": time},
        detail=detail,
        now="2026-10-04T12:00:00Z",
    )
    assert plan["status"] == "low_confidence", plan.get("why")
    assert plan["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP", plan.get("why")
    assert [gap["kind"] for gap in plan["why"]["details"]["gaps"]] == ["time_window_unrealized"]
    assert not plan.get("next", {}).get("ready_for")


@pytest.mark.parametrize("detail", ["best", "full", "query", "debug"])
@pytest.mark.parametrize(
    "time",
    [
        {"start": "2026-10-04"},
        {"grain": "day", "start": "2026-10-04"},
        {"end": "2026-10-05"},
    ],
)
def test_a_callers_open_bound_is_held(
    local_subscriptions: Runtime, time: dict[str, Any], detail: str
) -> None:
    # "today" drafts this day, which has no dated bound to lend: a caller's start alone is an
    # open window from that day on, and an end alone one up to it.
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={"time": time},
        detail=detail,
        now="2026-10-04T12:00:00Z",
    )
    _assert_window_gap(plan)


def test_a_whole_day_caller_window_executes(local_subscriptions: Runtime) -> None:
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={
            "time": {"start": "2026-10-04", "end": "2026-10-05"},
        },
        now="2026-10-04T12:00:00Z",
    )
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query"]
    rows = local_subscriptions.query(query)["rows"]
    gold = _signups(local_subscriptions, "2026-10-04", "2026-10-05")
    assert gold == 2
    assert sum(row[_alias(query["select"][0])] for row in rows) == gold


@pytest.mark.parametrize(
    "phrase",
    [
        "2026-01-01 to now",
        "January 2026 through now",
        "2026-01-01 to right now",
        "January 2026 through currently",
        "2026-01-01 to as of now",
        "now to 2026-01-01",
    ],
)
def test_a_range_ending_in_now_is_held_in_full(runtime_factory, phrase: str) -> None:
    window = _time_window(f"orders {phrase}", policy_context=NOW)
    assert window.bounds == {}
    assert window.windows == ()
    assert window.unresolved == (phrase.lower(),)
    plan = plan_payload(
        runtime_factory("jaffle_shop"),
        intent=f"orders {phrase}",
        partial_query={"policy_context": NOW},
    )
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
    assert plan["why"]["details"]["unresolved_phrases"] == [phrase.lower()]
    assert not (plan.get("best") or {}).get("query")


@pytest.mark.parametrize("phrase", ["last 10000 years", "last 9999999999 days"])
def test_unrepresentable_as_of_bounds_are_held(runtime_factory, phrase: str) -> None:
    runtime = runtime_factory("jaffle_shop")
    plan = plan_payload(
        runtime, intent=f"revenue end of {phrase}", partial_query={"policy_context": NOW}
    )
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
    assert plan["why"]["details"]["unresolved_phrases"] == [f"end of {phrase}"]
    assert not (plan.get("best") or {}).get("query")
    cue = _time_window(f"revenue end of {phrase}", policy_context=NOW).as_of[0]
    assert cue.bounds == {}
    # Ordinary relative intervals also return a held plan rather than escaping as an error.
    ordinary = plan_payload(
        runtime, intent=f"revenue {phrase}", partial_query={"policy_context": NOW}
    )
    assert ordinary["status"] != "ok"
    assert not ordinary["next"].get("ready_for")


@pytest.mark.parametrize(
    ("phrase", "unit", "start"),
    [
        ("today", "day", "2026-10-05"),
        ("this day", "day", "2026-10-05"),
        ("this week", "week", "2026-10-05"),
        ("this month", "month", "2026-10-01"),
        ("this quarter", "quarter", "2026-10-01"),
        ("this year", "year", "2026-01-01"),
    ],
)
def test_current_windows_use_the_query_clock(
    subscriptions: Runtime, wall_clock: None, phrase: str, unit: str, start: str
) -> None:
    plan = plan_payload(
        subscriptions, intent=f"new accounts {phrase}", partial_query={"policy_context": NOW}
    )
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query"]
    assert query["time"]["this"] == unit
    rows = subscriptions.query(query, now=NOW["now"])["rows"]
    # This period so far: from its start to now (06:00Z on October 5). The two October 5
    # signups count; the October 6 signup lies after now, so no window here counts it.
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        gold = connection.execute(
            "SELECT COUNT(DISTINCT event_id) FROM events WHERE kind = 'signup' "
            "AND occurred_at >= CAST(? AS DATE) "
            "AND CAST(occurred_at AS TIMESTAMP) < TIMESTAMP '2026-10-05 06:00:00'",
            [start],
        ).fetchone()[0]
    assert gold == 2
    assert sum(row[_alias(query["select"][0])] for row in rows) == gold


# Each as-of cue, with the day it reads a balance on.
AS_OF = {
    "now": "2026-10-04",
    "right now": "2026-10-04",
    "currently": "2026-10-04",
    "at the moment": "2026-10-04",
    "as of now": "2026-10-04",
    "current": "2026-10-04",
    "at the end of last month": "2026-09-30",
    "end of last month": "2026-09-30",
    "as of last month": "2026-09-30",
    "as of September 2026": "2026-09-30",
    "as of 2026-09-30": "2026-09-30",
}


@pytest.mark.parametrize("phrase", list(AS_OF))
def test_as_of_cues_are_held_in_full(subscriptions: Runtime, phrase: str) -> None:
    plan = plan_payload(
        subscriptions, intent=f"new accounts {phrase}", partial_query={"policy_context": NOW}
    )
    assert plan["status"] == "low_confidence", plan
    assert plan["why"]["code"] == "TIME_WINDOW_UNRESOLVED", plan.get("why")
    assert plan["why"]["details"]["unresolved_phrases"] == [phrase.lower()]
    assert not plan["next"].get("ready_for")
    assert not (plan.get("best") or {}).get("query")


@pytest.mark.parametrize(("phrase", "day"), list(AS_OF.items()))
def test_as_of_cues_read_a_balance_on_their_day(
    subscriptions: Runtime, phrase: str, day: str
) -> None:
    plan = plan_payload(
        subscriptions, intent=f"MRR {phrase}", partial_query={"policy_context": NOW}
    )
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query"]
    # The latest complete day is the last day; a named closing day is its dates.
    time = read_call(query, config=subscriptions._config, now=NOW["now"])[0]["time"]
    assert (query["time"]["grain"], time["start"]) == ("day", day)
    rows = subscriptions.query(query, now=NOW["now"])["rows"]
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        [(gold,)] = connection.execute(
            "SELECT SUM(mrr) FROM account_day WHERE day = ?", [day]
        ).fetchall()
    # The seed has rows for 2026-10-04 only: a day without rows returns none, never another.
    assert [row[_alias(query["select"][0])] for row in rows] == ([] if gold is None else [gold])


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
    assert not (plan.get("best") or {}).get("query")


def test_the_clock_is_scoped_and_the_cache_is_keyed_by_date(
    subscriptions: Runtime, wall_clock: None
) -> None:
    # "today" is this day on whichever clock reads the call: the draft carries no date.
    for context in (NOW, {"now": "2026-10-06"}):
        parsed = parse_intent(subscriptions, "new accounts today", policy_context=context)
        composed = compose(subscriptions, "new accounts today", policy_context=context)
        assert parsed.time["this"] == "day"
        assert composed.draft.query.time.this == "day"
    assert _time_window("new accounts today").bounds == {"this": "day"}
    # A day an as-of cue names is read on the caller's clock, and a later read without one
    # is on the wall clock: the previous caller cannot change it.
    cue = _time_window("MRR end of last month", policy_context=NOW).as_of[0]
    assert cue.bounds == {"start": "2026-09-01", "end": "2026-10-01"}
    cue = _time_window("MRR end of last month").as_of[0]
    assert cue.bounds == {"start": "2031-01-01", "end": "2031-02-01"}


def test_faithfulness_resolves_relative_windows_on_the_query_clock() -> None:
    windows = [((0, 9), {"last": {"unit": "day", "value": 1}})]
    assert _window_agrees(windows, {"start": "2026-10-04", "end": "2026-10-05"}, NOW)
    assert not _window_agrees(windows, {"start": "2026-10-05", "end": "2026-10-06"}, NOW)


def test_a_forced_wrong_day_draft_is_held(
    subscriptions: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    result = compose(subscriptions, "new accounts today", policy_context=NOW)
    wrong = replace(
        result.draft.query,
        time=result.draft.query.time.with_window({"start": "2026-10-06", "end": "2026-10-07"}),
    )
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
    query = plan["best"]["query"]
    assert query["time"]["this"] == "day"
