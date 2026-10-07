"""Planning uses the caller's clock and holds unconsumed as-of cues."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.ast import normalize_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.planner import plan_payload
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.orchestrator import compose
from semantic_rails.planner.time_checks import _window_agrees
from semantic_rails.planner.time_windows import _time_window
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


def _set_wall_clock(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    class MachineDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(moment.year, moment.month, moment.day)

    monkeypatch.setattr("semantic_rails.planner.time_windows.date", MachineDate)

    class MachineDatetime(datetime):
        @classmethod
        def now(cls, tz=None) -> datetime:
            return cls.combine(moment.date(), moment.timetz())

    monkeypatch.setattr("semantic_rails.planner.time_windows.datetime", MachineDatetime)
    monkeypatch.setattr("semantic_rails.ast.datetime", MachineDatetime)


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


def _assert_zone_hold(plan: dict[str, Any], phrases: list[str]) -> None:
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "TIME_WINDOW_UNRESOLVED", plan.get("why")
    assert plan["why"]["details"]["unresolved_phrases"] == phrases
    assert not plan.get("next", {}).get("ready_for")
    assert not (plan.get("best") or {}).get("query_ir")
    assert not any(
        row.get("query_ir") for row in plan.get("alternatives", []) + plan.get("blocked", [])
    )


def test_today_is_held_where_the_roles_zone_reads_another_day(
    local_subscriptions: Runtime,
) -> None:
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={"policy_context": {"now": "2026-10-05T01:00:00Z"}},
    )
    _assert_zone_hold(plan, ["today"])
    details = plan["why"]["details"]
    assert (details["timezone"], details["planning_timezone"]) == ("America/New_York", "UTC")


def test_today_executes_where_both_zones_read_the_same_day(local_subscriptions: Runtime) -> None:
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={"policy_context": {"now": "2026-10-05T12:00:00Z"}},
    )
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query_ir"]
    assert (query["time"]["start"], query["time"]["end"]) == ("2026-10-05", "2026-10-06")
    rows = local_subscriptions.query(query)["rows"]
    gold = _signups(local_subscriptions, "2026-10-05", "2026-10-06")
    assert gold == 1
    assert sum(row[query["select"][0]["as"]] for row in rows) == gold


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
def test_orders_windows_are_held_where_the_roles_zone_reads_other_days(
    local_orders: Runtime, phrase: str, now: str
) -> None:
    plan = plan_payload(
        local_orders, intent=f"orders {phrase}", partial_query={"policy_context": {"now": now}}
    )
    _assert_zone_hold(plan, [phrase])


@pytest.mark.parametrize(
    ("phrase", "now", "start", "end"),
    [
        ("today", "2026-10-05T12:00:00Z", "2026-10-05", "2026-10-06"),
        ("yesterday", "2026-10-05T12:00:00Z", "2026-10-04", "2026-10-05"),
        ("today", "2026-10-05T01:00:00", "2026-10-05", "2026-10-06"),
        ("yesterday", "2026-10-05T01:00:00", "2026-10-04", "2026-10-05"),
        ("today", "2026-10-05", "2026-10-05", "2026-10-06"),
    ],
)
def test_orders_windows_run_as_returned_where_both_zones_agree(
    local_orders: Runtime, phrase: str, now: str, start: str, end: str
) -> None:
    plan = plan_payload(
        local_orders, intent=f"orders {phrase}", partial_query={"policy_context": {"now": now}}
    )
    assert plan["status"] == "ok", plan.get("why")
    # The returned query runs without the caller's clock.
    time = normalize_query(plan["best"]["query_ir"], config=local_orders._config).time
    assert (time.start, time.end) == (start, end)


def test_the_wall_clock_is_held_where_the_roles_zone_reads_another_day(
    local_subscriptions: Runtime, wall_clock: None
) -> None:
    plan = plan_payload(local_subscriptions, intent="new accounts today")
    _assert_zone_hold(plan, ["today"])


def test_a_period_comparison_is_held_where_the_roles_zone_reads_another_year(
    local_orders: Runtime,
) -> None:
    plan = plan_payload(
        local_orders,
        intent="monthly revenue this year compared to last year",
        partial_query={"policy_context": {"now": "2027-01-01T02:00:00Z"}},
    )
    _assert_zone_hold(plan, ["this year"])
    # No query is returned, so there are no rows to filter.
    assert "best.query_ir" not in str(plan["why"])


def test_a_period_comparison_drops_its_start_where_both_zones_agree(local_orders: Runtime) -> None:
    plan = plan_payload(
        local_orders,
        intent="monthly revenue this year compared to last year",
        partial_query={"policy_context": {"now": "2026-07-01T12:00:00Z"}},
    )
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "TIME_WINDOW_START_DROPPED", plan.get("why")
    assert plan["why"]["details"]["requested_start"] == "2026-01-01"
    time = plan["best"]["query_ir"]["time"]
    assert "start" not in time and time["end"] == "2027-01-01"


@pytest.mark.parametrize("caller_window", [False, True])
def test_a_package_zone_reading_is_held_where_the_role_reads_utc(
    local_subscriptions: Runtime, caller_window: bool
) -> None:
    # Tokyo reads 2026-10-06; New York and UTC both read 2026-10-05.
    _with_package_zone(local_subscriptions, "Asia/Tokyo")
    window = {"time": {"start": "2026-10-05", "end": "2026-10-06"}} if caller_window else {}
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={"policy_context": {"now": "2026-10-05T16:00:00Z"}, **window},
    )
    _assert_zone_hold(plan, ["today"])
    assert plan["why"]["details"]["planning_timezone"] == "Asia/Tokyo"


@pytest.mark.parametrize(
    ("start", "end"), [("2026-10-06", "2026-10-07"), ("2026-10-05", "2026-10-06")]
)
def test_a_caller_window_must_agree_in_the_planning_zone_and_utc(
    subscriptions: Runtime, start: str, end: str
) -> None:
    # Tokyo is both the role's and the planning zone and reads 2026-10-06; UTC reads
    # 2026-10-05. A window held in UTC stays held; one read in UTC is another local day.
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
            "policy_context": {"now": "2026-10-05T16:00:00Z"},
            "time": {"start": start, "end": end},
        },
    )
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert not plan["next"].get("ready_for")


def test_a_callers_range_is_held_where_the_roles_zone_reads_other_days(
    subscriptions: Runtime,
) -> None:
    # "last month" reads September in UTC and Tokyo; the last 30 days end on October 1 in UTC
    # but on October 2 in Tokyo.
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
            "policy_context": {"now": "2026-10-01T16:00:00Z"},
            "time": {"range": {"last": {"unit": "day", "value": 30}}},
        },
    )
    _assert_zone_hold(plan, ["last month"])


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


def _shop_value(runtime: Runtime, plan: dict[str, Any]) -> Any:
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query_ir"]
    [row] = runtime.query(query)["rows"]
    return row[query["select"][0]["as"]]


def _shop_reference(runtime: Runtime, sql: str) -> Any:
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        return connection.execute(sql).fetchone()[0]


@pytest.mark.parametrize("surface", ["python", "mcp"])
@pytest.mark.parametrize("detail", ["best", "full", "query", "debug"])
@pytest.mark.parametrize("phrase", ["yesterday", "today"])
def test_a_window_a_leg_reads_on_other_days_in_its_zone_is_held(
    shop: Runtime, monkeypatch: pytest.MonkeyPatch, surface: str, detail: str, phrase: str
) -> None:
    # Sessions are read in UTC, orders on New York's day. At 01:00Z New York's "yesterday" is
    # October 3, but the UTC dates would read orders on New York's October 4; its "today" is
    # October 4, but they would read October 5, New York's tomorrow.
    plan = _plan_on(shop, monkeypatch, surface, f"purchase rate {phrase}", detail=detail)
    _assert_zone_hold(plan, [phrase])
    assert not plan.get("query_ir")
    details = plan["why"]["details"]
    assert details["temporal_roles"] == {SHOP_ORDERS: "America/New_York"}
    assert (details["temporal_role"], details["timezone"]) == (SHOP_ORDERS, "America/New_York")
    assert details["planning_timezone"] == "UTC"


def test_a_window_every_leg_reads_on_the_same_days_executes(shop: Runtime) -> None:
    # At 12:00Z UTC and New York both read October 4 as yesterday.
    plan = plan_payload(
        shop,
        intent="purchase rate yesterday",
        partial_query={"policy_context": {"now": "2026-10-05T12:00:00Z"}},
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
    assert _shop_value(shop, plan) == reference


def test_an_absolute_window_on_a_mixed_zone_metric_is_unchanged(shop: Runtime) -> None:
    plan = plan_payload(
        shop,
        intent="purchase rate on 4 October 2026",
        partial_query={"policy_context": {"now": SHOP_NOW.isoformat()}},
    )
    assert _shop_value(shop, plan) == 0.25


def test_a_metric_whose_legs_share_the_planning_zone_is_unchanged(shop: Runtime) -> None:
    plan = plan_payload(
        shop,
        intent="session count yesterday",
        partial_query={"policy_context": {"now": SHOP_NOW.isoformat()}},
    )
    reference = _shop_reference(
        shop,
        "SELECT COUNT(DISTINCT session_id) FROM sessions "
        "WHERE started_at >= TIMESTAMP '2026-10-04' AND started_at < TIMESTAMP '2026-10-05'",
    )
    assert reference == 4
    assert _shop_value(shop, plan) == reference


def test_roles_that_cannot_be_computed_are_held(
    shop: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unbound(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("binding failed")

    monkeypatch.setattr("semantic_rails.planner.time_checks.bind_query", unbound)
    plan = plan_payload(
        shop,
        intent="session count yesterday",
        partial_query={"policy_context": {"now": SHOP_NOW.isoformat()}},
    )
    _assert_zone_hold(plan, ["yesterday"])


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
    assert parsed.time["start"] == "2026-10-04"
    assert set(dates) == {date(2026, 10, 4)}


def test_the_cache_tracks_local_midnight_within_one_utc_day() -> None:
    for now, expected in [
        ("2026-10-05T03:59:00Z", "2026-10-04"),
        ("2026-10-05T04:01:00Z", "2026-10-05"),
    ]:
        assert (
            _time_window("today", {"now": now}, timezone="America/New_York").bounds["start"]
            == expected
        )
        assert _time_window("today", {"now": now}, timezone="UTC").bounds["start"] == "2026-10-05"


def test_relative_and_absolute_windows_agree_in_the_roles_zone() -> None:
    windows = [((0, 9), {"range": {"last": {"unit": "day", "value": 1}}})]
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
    wrong_time = {**result.draft.query["time"], "start": start, "end": end}
    wrong_time.pop("range", None)
    wrong = {**result.draft.query, "time": wrong_time}
    monkeypatch.setattr(
        "semantic_rails.planner.plan.compose",
        lambda *_args, **_kwargs: replace(result, draft=replace(result.draft, query=wrong)),
    )
    partial = {"policy_context": context, **({"time": wrong_time} if caller_window else {})}
    plan = plan_payload(
        local_subscriptions, intent=f"new accounts {phrase}", partial_query=partial, detail=detail
    )
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "TIME_WINDOW_UNRESOLVED", plan.get("why")
    assert plan["why"]["details"]["unresolved_phrases"] == [phrase]
    assert not plan.get("next", {}).get("ready_for")
    assert not (plan.get("best") or {}).get("query_ir")
    assert not plan.get("query_ir")
    assert not any(
        row.get("query_ir") for row in plan.get("alternatives", []) + plan.get("blocked", [])
    )


def test_a_previously_held_explicit_local_window_stays_held(local_subscriptions: Runtime) -> None:
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={
            "policy_context": {"now": "2026-10-05T01:00:00Z"},
            "time": {"start": "2026-10-04", "end": "2026-10-05"},
        },
    )
    _assert_zone_hold(plan, ["today"])


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
        partial_query={"policy_context": {"now": "2026-10-05T12:00:00Z"}},
    )
    assert plan["status"] == "ok", plan.get("why")
    time = plan["best"]["query_ir"]["time"]
    assert (time["start"], time["end"]) == ("2026-10-05", "2026-10-06")


@pytest.mark.parametrize("detail", ["best", "full", "query", "debug"])
def test_a_returned_relative_window_keeps_the_supplied_clock(
    local_subscriptions: Runtime, monkeypatch: pytest.MonkeyPatch, detail: str
) -> None:
    _set_wall_clock(monkeypatch, datetime(2026, 10, 7, 12, tzinfo=UTC))
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts yesterday",
        partial_query={"policy_context": {"now": "2026-10-05T12:00:00Z"}},
        detail=detail,
    )
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query_ir"]
    assert "range" not in query["time"] and "policy_context" not in query
    # Run unchanged, without the clock it was planned on.
    rows = local_subscriptions.query(query)["rows"]
    with duckdb.connect(local_subscriptions.db_path, read_only=True) as connection:
        gold = connection.execute(
            "SELECT COUNT(DISTINCT event_id) FROM events WHERE kind='signup' "
            "AND occurred_at >= DATE '2026-10-04' AND occurred_at < DATE '2026-10-05'"
        ).fetchone()[0]
    assert gold == 2
    assert sum(row[query["select"][0]["as"]] for row in rows) == gold
    assert not any(
        "range" in (row.get("query_ir") or {}).get("time", {})
        for row in plan.get("alternatives", []) + plan.get("blocked", [])
    )


def test_a_relative_window_the_clock_cannot_bound_is_held(
    local_subscriptions: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unbounded(*_args: Any, **_kwargs: Any) -> Any:
        raise SemanticLayerError("INVALID_QUERY", "unbounded")

    monkeypatch.setattr("semantic_rails.planner.intent_holds._time_spec_from_payload", unbounded)
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts yesterday",
        partial_query={"policy_context": {"now": "2026-10-05T12:00:00Z"}},
    )
    _assert_zone_hold(plan, ["yesterday"])


@pytest.mark.parametrize("detail", ["best", "full", "query", "debug"])
def test_offset_bounds_on_another_local_day_are_held(
    local_subscriptions: Runtime, detail: str
) -> None:
    # Both bounds fall on 2026-10-03 in New York.
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={
            "policy_context": {"now": "2026-10-04T12:00:00Z"},
            "time": {"start": "2026-10-04T01:00:00Z", "end": "2026-10-04T02:00:00Z"},
        },
        detail=detail,
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
            "policy_context": {"now": "2026-10-04T12:00:00Z"},
            "time": {"start": start, "end": end},
        },
        detail=detail,
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
        {"grain": "", "start": "2026-10-04", "end": None},
        {"grain": "day", "start": "2026-10-04", "end": None},
        {"start": None, "end": "2026-10-05"},
        # Without its start the window also reads October 3.
        {"grain": "", "start": None, "end": "2026-10-05"},
    ],
)
def test_a_caller_window_that_clears_a_bound_is_held(
    local_subscriptions: Runtime, time: dict[str, Any], detail: str
) -> None:
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={"policy_context": {"now": "2026-10-04T12:00:00Z"}, "time": time},
        detail=detail,
    )
    assert plan["status"] == "low_confidence", plan.get("why")
    assert plan["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP", plan.get("why")
    assert [gap["kind"] for gap in plan["why"]["details"]["gaps"]] == ["time_window_unrealized"]
    assert not plan.get("next", {}).get("ready_for")


@pytest.mark.parametrize("detail", ["best", "full", "query", "debug"])
@pytest.mark.parametrize(
    "time",
    [
        {"grain": "", "start": "2026-10-04"},
        {"grain": "day", "start": "2026-10-04"},
        {"end": "2026-10-05"},
    ],
)
def test_an_omitted_caller_bound_keeps_the_drafted_one(
    local_subscriptions: Runtime, time: dict[str, Any], detail: str
) -> None:
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={"policy_context": {"now": "2026-10-04T12:00:00Z"}, "time": time},
        detail=detail,
    )
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query_ir"]
    assert (query["time"]["start"], query["time"]["end"]) == ("2026-10-04", "2026-10-05")
    rows = local_subscriptions.query(query)["rows"]
    gold = _signups(local_subscriptions, "2026-10-04", "2026-10-05")
    assert gold == 2
    assert sum(row[query["select"][0]["as"]] for row in rows) == gold


def test_a_whole_day_caller_window_executes(local_subscriptions: Runtime) -> None:
    plan = plan_payload(
        local_subscriptions,
        intent="new accounts today",
        partial_query={
            "policy_context": {"now": "2026-10-04T12:00:00Z"},
            "time": {"start": "2026-10-04", "end": "2026-10-05"},
        },
    )
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query_ir"]
    rows = local_subscriptions.query(query)["rows"]
    gold = _signups(local_subscriptions, "2026-10-04", "2026-10-05")
    assert gold == 2
    assert sum(row[query["select"][0]["as"]] for row in rows) == gold


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
    assert not (plan.get("best") or {}).get("query_ir")


@pytest.mark.parametrize("phrase", ["last 10000 years", "last 9999999999 days"])
def test_unrepresentable_as_of_bounds_are_held(runtime_factory, phrase: str) -> None:
    runtime = runtime_factory("jaffle_shop")
    plan = plan_payload(
        runtime, intent=f"revenue end of {phrase}", partial_query={"policy_context": NOW}
    )
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "TIME_WINDOW_UNRESOLVED"
    assert plan["why"]["details"]["unresolved_phrases"] == [f"end of {phrase}"]
    assert not (plan.get("best") or {}).get("query_ir")
    cue = _time_window(f"revenue end of {phrase}", policy_context=NOW).as_of[0]
    assert cue.bounds == {}
    # Ordinary relative intervals also return a held plan rather than escaping as an error.
    ordinary = plan_payload(
        runtime, intent=f"revenue {phrase}", partial_query={"policy_context": NOW}
    )
    assert ordinary["status"] != "ok"
    assert not ordinary["next"].get("ready_for")


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
