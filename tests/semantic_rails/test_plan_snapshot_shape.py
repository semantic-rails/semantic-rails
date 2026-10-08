"""plan reads a balance on the one day the question names.

A subscriptions package: accounts a, b and d are customers, c is internal. Every account has a
daily row from 2026-09-01 to 2026-10-04: 99 a day on the basic plan, except that b pays 0 from
2026-10-01 and a moves to pro at 500 from 2026-10-02. ``mrr`` and ``paying_accounts`` are
balances filtered to customers. Account e is a nonpaying pro customer on 2026-10-04. The
policy variant reads them per day (a metric constraint
requires the day grouping, and the clock supports only days); the plain variant declares
neither. The clock is 2026-10-05T06:00Z, so the last complete day is 2026-10-04. Every answer
is checked against plain SQL on the seed.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.planner import plan as plan_module
from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime

NOW = {"now": "2026-10-05T06:00:00Z"}
NS = "subscriptions"
CLOCK = f"temporal_role.{NS}_account_day_day"
DAY = f"dimension.{NS}_account_day_day"
PLAN = f"dimension.{NS}_account_day_plan"
NAME = f"dimension.{NS}_account_name"
MRR = f"metric.{NS}.mrr"
SEED = """
CREATE TABLE accounts (account_id VARCHAR, name VARCHAR, segment VARCHAR);
INSERT INTO accounts VALUES ('a', 'Acme Data Co', 'customer'), ('b', 'Globex', 'customer'),
  ('c', 'QA Sandbox', 'internal'), ('d', 'Initech', 'customer');
CREATE TABLE events (event_id INTEGER, account_id VARCHAR, kind VARCHAR, occurred_at DATE);
INSERT INTO events VALUES (1, 'a', 'signup', '2026-09-02'), (2, 'b', 'signup', '2026-09-22'),
  (3, 'c', 'signup', '2026-09-29'), (4, 'd', 'signup', '2026-09-30');
CREATE TABLE account_day AS
SELECT account_id, day, plan, mrr, CAST(mrr > 0 AS INTEGER) AS paying FROM (
  SELECT a.account_id, CAST(d AS DATE) AS day,
    CASE WHEN a.account_id = 'a' AND d >= DATE '2026-10-02' THEN 'pro' ELSE 'basic' END AS plan,
    CAST(CASE WHEN a.account_id = 'b' AND d >= DATE '2026-10-01' THEN 0
              WHEN a.account_id = 'a' AND d >= DATE '2026-10-02' THEN 500 ELSE 99 END AS DOUBLE)
      AS mrr
  FROM accounts a, range(DATE '2026-09-01', DATE '2026-10-05', INTERVAL 1 DAY) t(d));
INSERT INTO accounts VALUES ('e', 'Nonpaying Pro', 'customer');
INSERT INTO account_day VALUES ('e', DATE '2026-10-04', 'pro', 0, 0);
"""
CUSTOMERS = {"field": f"dimension.{NS}_account_segment", "op": "=", "value": "customer"}
DAY_POLICY = {
    "id": "policy.subscriptions.balance_per_day",
    "kind": "metric_constraint",
    "object_ids": [f"measure.{NS}.mrr_all", f"measure.{NS}.paying_all"],
    "required_group_by": [DAY],
}


def _package(root: Path, *, policies: list[dict[str, Any]], days_only: bool) -> Path:
    def put(name: str, doc: dict[str, Any]) -> None:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    stock = {"kind": "stock", "snapshot": "end_of_period"}
    day = {"column": "day", "kind": "date", "class": "as_of_time", "default": True}
    put("package.yml", {
        "schema_version": 1,
        "package": {"id": NS, "namespace": NS, "name": "Subscriptions",
                    "description": "Account activity and balances", "warehouse": "duckdb",
                    "default_db": "subscriptions.duckdb", "seed": {"kind": "external"}},
        "defaults": {"time": {"timezone": "UTC"}},
    })  # fmt: skip
    put("graph.yml", {"graph": {"entities": {
        "account": {"key": ["account_id"], "model": "accounts"},
        "event": {"key": ["event_id"], "model": "events"},
        "account_day": {"key": ["account_id", "day"], "model": "account_day"},
    }}})  # fmt: skip
    put("models/accounts.yml", {"model": {
        "id": "accounts", "relation": "accounts", "entities": {"account": {}},
        "dimensions": {"name": {"kind": "categorical"},
                       "segment": {"kind": "categorical", "domain": ["customer", "internal"]}},
    }})  # fmt: skip
    put("models/events.yml", {"model": {
        "id": "events", "relation": "events", "entities": {"event": {}, "account": {}},
        "times": {"occurred_at": {"column": "occurred_at", "kind": "date",
                                  "class": "event_time", "default": True}},
        "dimensions": {"kind": {"kind": "categorical", "domain": ["signup"]}},
        "measures": {"events_all": {"kind": "entity_count", "entity_key": "event_id",
                                    "value_type": "count", "publish": False}},
    }})  # fmt: skip
    put("models/account_day.yml", {"model": {
        "id": "account_day", "relation": "account_day",
        "entities": {"account_day": {}, "account": {}},
        "times": {"day": {**day, **({"supported_grains": ["day"]} if days_only else {})}},
        "dimensions": {"plan": {"kind": "categorical", "domain": ["basic", "pro"]}},
        "measures": {
            "mrr_all": {"expr": "mrr", "accumulation": stock, "publish": False},
            "paying_all": {"expr": "paying", "accumulation": stock, "publish": False},
        },
    }})  # fmt: skip
    put("metrics/accounts.yml", {"metrics": {
        "new_accounts": {
            "label": "New accounts", "kind": "aggregate", "value_type": "count",
            "temporal_role": f"temporal_role.{NS}_event_occurred_at",
            "expression": {"kind": "aggregate", "measure": f"measure.{NS}.events_all",
                           "aggregation": "count_distinct", "filter": {"all": [CUSTOMERS]}},
        },
        "mrr": {
            "label": "MRR (USD)", "kind": "semi_additive", "temporal_role": CLOCK,
            "expression": {"kind": "semi_additive", "measure": f"measure.{NS}.mrr_all",
                           "filter": {"all": [CUSTOMERS]}},
        },
        "paying_accounts": {
            "label": "Paying accounts", "kind": "semi_additive", "value_type": "count",
            "temporal_role": CLOCK,
            "expression": {"kind": "semi_additive", "measure": f"measure.{NS}.paying_all",
                           "filter": {"all": [CUSTOMERS]}},
        },
    }})  # fmt: skip
    if policies:
        put("policies.yml", {"semantic_policies": policies})
    with duckdb.connect(str(root / "subscriptions.duckdb")) as connection:
        connection.execute(SEED)
    return root


def _runtime(root: Path, **kwargs: Any) -> Runtime:
    return Runtime.from_path(str(_package(root, **kwargs)))


@pytest.fixture(scope="module", params=["policy", "plain"])
def runtime(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    policy = request.param == "policy"
    engine = _runtime(
        tmp_path_factory.mktemp(request.param) / NS,
        policies=[DAY_POLICY] if policy else [],
        days_only=policy,
    )
    try:
        engine._get_adapter()  # the module fixture owns its connection across tests
        yield engine
    finally:
        engine.close()


def _plan(runtime: Runtime, intent: str, now: dict[str, str] = NOW) -> dict[str, Any]:
    return plan_payload(runtime, intent=intent, partial_query={"policy_context": now})


def _reference(sql: str) -> dict[Any, float]:
    """``{group: value}`` from plain SQL on the seed (group ``()`` for one total)."""

    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        rows = connection.execute(sql).fetchall()
    return {
        row[:-1] if len(row) > 2 else (row[0] if len(row) == 2 else ()): row[-1] for row in rows
    }


def _balance(column: str, day: str, by: str = "") -> str:
    group = f"{by}, " if by else ""
    return (
        f"SELECT {group}SUM({column}) FROM account_day JOIN accounts USING (account_id) "
        f"WHERE segment = 'customer' AND day = DATE '{day}'" + (f" GROUP BY {by}" if by else "")
    )


def _answer(runtime: Runtime, plan: dict[str, Any], by: str = "") -> dict[Any, float]:
    query = plan["best"]["query_ir"]
    alias = query["select"][0]["as"]
    rows = runtime.query({**query, "policy_context": NOW})["rows"]
    return {(row[by] if by else ()): row[alias] for row in rows}


@pytest.mark.parametrize(
    ("intent", "day", "column", "by"),
    [
        ("What's our MRR?", "2026-10-04", "mrr", ""),
        ("MRR right now", "2026-10-04", "mrr", ""),
        ("What was MRR at the end of last month?", "2026-09-30", "mrr", ""),
        ("MRR last month", "2026-09-30", "mrr", ""),
        ("MRR as of 2026-09-30", "2026-09-30", "mrr", ""),
        ("MRR by plan", "2026-10-04", "mrr", "plan"),
        ("How many paying accounts do we have?", "2026-10-04", "paying", ""),
        ("paying accounts by plan", "2026-10-04", "paying", "plan"),
    ],
)
def test_a_balance_reads_the_day_the_question_names(
    runtime: Runtime, intent: str, day: str, column: str, by: str
) -> None:
    plan = _plan(runtime, intent)
    assert plan["status"] == "ok", plan.get("why")
    assert plan["next"]["ready_for"] == ["execute"]
    query = plan["best"]["query_ir"]
    end = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    assert query["time"] == {"temporal_role": CLOCK, "grain": "day", "start": day, "end": end}
    reference = _reference(_balance(column, day, by))
    answer = _answer(runtime, plan, PLAN if by else "")
    assert answer == reference
    policy = bool(runtime._config.semantic_policies)
    assert (DAY in query.get("group_by", [])) is policy
    if intent != "MRR as of 2026-09-30":
        [assumption] = plan["assumptions"]
        assert day in assumption


def test_the_numbers_are_the_customers_balance_on_that_day(runtime: Runtime) -> None:
    # The building block counts every segment; the metric counts customers.
    assert _reference(_balance("mrr", "2026-10-04").replace("segment = 'customer' AND ", "")) == {
        (): 698
    }
    assert _reference(_balance("mrr", "2026-10-04")) == {(): 599}
    assert _reference(_balance("mrr", "2026-09-30")) == {(): 297}
    assert _reference(_balance("mrr", "2026-10-04", "plan")) == {"basic": 99, "pro": 500}
    assert _reference(_balance("paying", "2026-10-04")) == {(): 2}
    assert _reference(_balance("paying", "2026-10-04", "plan")) == {"basic": 1, "pro": 1}
    plan = _plan(runtime, "What's our MRR?")
    assert plan["best"]["query_ir"]["select"] == [{"as": "mrr", "expression": {"metric": MRR}}]


def test_right_now_is_the_same_read_as_no_time_words(runtime: Runtime) -> None:
    asked, plain = _plan(runtime, "MRR right now"), _plan(runtime, "What's our MRR?")
    for key in ("select", "time", "group_by"):
        assert asked["best"]["query_ir"].get(key) == plain["best"]["query_ir"].get(key)
    assert _answer(runtime, asked) == _answer(runtime, plain) == {(): 599}


def test_a_caller_select_keeps_its_measure_and_alias(runtime: Runtime) -> None:
    select = [{"as": "all_mrr", "expression": {
        "measure": f"measure.{NS}.mrr_all", "aggregation": "last_value",
    }}]  # fmt: skip
    plan = plan_payload(runtime, intent="What's our MRR?", partial_query={
        "policy_context": NOW, "select": select,
        "order_by": [{"field": "all_mrr", "direction": "DESC"}],
    })  # fmt: skip
    assert plan["status"] == "ok", plan.get("why")
    assert plan["next"]["ready_for"] == ["execute"]
    query = plan["best"]["query_ir"]
    assert query["select"] == select
    assert query["order_by"] == [{"field": "all_mrr", "direction": "DESC"}]
    assert (
        _answer(runtime, plan)
        == _reference("SELECT SUM(mrr) FROM account_day WHERE day = DATE '2026-10-04'")
        == {(): 698}
    )
    assert _answer(runtime, _plan(runtime, "What's our MRR?")) == {(): 599}


@pytest.mark.parametrize("intent", [
    "MRR all time", "all-time MRR", "MRR ever", "MRR to date", "MRR since launch",
    "MRR trend", "MRR history", "MRR historical", "MRR lifetime", "MRR peak",
    "MRR trends", "MRR trending",
])  # fmt: skip
def test_time_words_without_one_day_stay_held(runtime: Runtime, intent: str) -> None:
    plan = _plan(runtime, intent)
    assert plan["status"] in {"low_confidence", "needs_clarification"}, plan.get("why")
    assert "execute" not in plan["next"].get("ready_for", [])
    assert "stock_as_of_unrealized" in {gap["kind"] for gap in plan["why"]["details"]["gaps"]}


@pytest.mark.parametrize(("intent", "start", "end"), [
    ("MRR today", "", ""),
    ("What's our MRR?", "2026-10-05", "2026-10-06"),
    ("What's our MRR?", "2026-10-06", "2026-10-07"),
    ("What's our MRR?", "2026-10-04", "2026-10-06"),
])  # fmt: skip
def test_an_incomplete_balance_day_is_held(
    runtime: Runtime, intent: str, start: str, end: str
) -> None:
    partial: dict[str, Any] = {"policy_context": NOW}
    if runtime._config.semantic_policies:
        partial["group_by"] = [DAY]
    if start:
        partial["time"] = {"temporal_role": CLOCK, "grain": "day", "start": start, "end": end}
    plan = plan_payload(runtime, intent=intent, partial_query=partial)
    assert plan["status"] == "low_confidence", plan.get("why")
    assert "execute" not in plan["next"].get("ready_for", [])
    [gap] = [gap for gap in plan["why"]["details"]["gaps"]
             if gap["kind"] == "stock_as_of_unrealized"]  # fmt: skip
    assert "isn't complete yet" in gap["message"]
    assert (start if start > "2026-10-05" else "2026-10-05") in gap["message"]
    assert any("2026-10-04" in hint["message"] for hint in plan["why"]["recovery_hints"])


def test_a_value_named_with_the_balance_filters_it(runtime: Runtime) -> None:
    all_pro = _reference(
        "SELECT COUNT(*) FROM account_day JOIN accounts USING (account_id) "
        "WHERE segment = 'customer' AND day = DATE '2026-10-04' AND plan = 'pro'"
    )
    paying_pro = {(): _reference(_balance("paying", "2026-10-04", "plan"))["pro"]}
    assert all_pro == {(): 2}
    assert paying_pro == {(): 1}
    ambiguous = _plan(runtime, "pro accounts right now")
    assert ambiguous["status"] != "ok", ambiguous.get("why")
    assert "execute" not in ambiguous["next"].get("ready_for", [])
    assert "subject_ambiguous" in {gap["kind"] for gap in ambiguous["why"]["details"]["gaps"]}
    plan = _plan(runtime, "pro paying accounts right now")
    assert plan["status"] == "ok", plan.get("why")
    assert plan["best"]["query_ir"]["where"] == [{"field": PLAN, "op": "=", "value": "pro"}]
    assert "Paying accounts" in " ".join(plan["assumptions"])
    assert _answer(runtime, plan) == paying_pro


def test_trials_ending_this_week_stay_held(runtime: Runtime) -> None:
    plan = _plan(runtime, "trials ending this week")
    assert plan["status"] != "ok", plan.get("why")
    assert "execute" not in plan["next"].get("ready_for", [])


def test_a_series_is_asked_about_under_a_per_day_policy(runtime: Runtime) -> None:
    plan = _plan(runtime, "MRR by week")
    assert "execute" not in plan["next"].get("ready_for", [])
    [gap] = plan["why"]["details"]["gaps"]
    assert gap["kind"] == "stock_as_of_unrealized"
    if runtime._config.semantic_policies:
        assert plan["status"] == "needs_clarification"
        assert plan["next"] == {"action": "clarify"}
        assert plan["why"]["details"]["clarification"]["question"]
        assert "pick a day" in plan["why"]["recovery_hints"][0]["message"].lower()
    else:
        # The plain package allows weeks, but a week reads each series' last value: held.
        assert plan["status"] == "low_confidence"
        assert gap["actual"] == {"grain": "week"}


def test_a_compared_balance_is_asked_about(runtime: Runtime) -> None:
    plan = _plan(runtime, "MRR right now compared with a week ago")
    assert plan["status"] == "needs_clarification", plan.get("why")
    assert plan["next"] == {"action": "clarify"}
    [gap] = plan["why"]["details"]["gaps"]
    assert gap["kind"] == "stock_as_of_unrealized"


@pytest.mark.parametrize(
    ("intent", "code"),
    [
        # A flow has no as-of day.
        ("new accounts right now", "TIME_WINDOW_UNRESOLVED"),
        # A day not yet complete is never read, nor an earlier one instead.
        ("MRR at the end of this month", "TIME_WINDOW_UNRESOLVED"),
        ("paying accounts this week", None),
        # Several periods are a series, not one as-of day.
        ("MRR last 3 months", None),
    ],
)
def test_no_single_complete_day_stays_held(runtime: Runtime, intent: str, code: str | None) -> None:
    plan = _plan(runtime, intent)
    assert plan["status"] == "low_confidence", plan
    assert "execute" not in plan["next"].get("ready_for", [])
    if code and plan["best"]["validation_ok"]:
        assert plan["why"]["code"] == code


def test_an_unloaded_last_complete_day_returns_no_rows(runtime: Runtime) -> None:
    later = {"now": "2026-10-06T06:00:00Z"}
    plan = _plan(runtime, "What's our MRR?", later)
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query_ir"]
    assert (query["time"]["start"], query["time"]["end"]) == ("2026-10-05", "2026-10-06")
    result = runtime.query({**query, "policy_context": later})
    assert result["rows"] == []
    assert "EMPTY_RESULT_WINDOW" in {warning["code"] for warning in result["warnings"]}


def test_a_draft_forced_past_the_shaper_is_held(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unshaped(_runtime: Any, _question: str, query: dict[str, Any], *_: Any, **__: Any):
        return query, None

    monkeypatch.setattr(plan_module, "shape_snapshot", unshaped)
    plan = _plan(runtime, "What's our MRR?")
    assert plan["status"] == "low_confidence"
    assert "execute" not in plan["next"].get("ready_for", [])
    if not runtime._config.semantic_policies:
        assert "stock_as_of_unrealized" in {gap["kind"] for gap in plan["why"]["details"]["gaps"]}


@pytest.mark.parametrize(("intent", "code"), [
    ("What's our MRR?", "PLAN_INTENT_COVERAGE_GAP"),
    ("MRR right now", "TIME_WINDOW_UNRESOLVED"),
])  # fmt: skip
def test_a_draft_shaped_to_another_day_is_held(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, intent: str, code: str
) -> None:
    shaper = plan_module.shape_snapshot

    def earlier(*args: Any, **kwargs: Any):
        query, why = shaper(*args, **kwargs)
        if query.get("time", {}).get("temporal_role") != CLOCK:
            return query, why
        moved = {"temporal_role": CLOCK, "grain": "day", "start": "2026-10-03", "end": "2026-10-04"}
        return {**query, "time": moved}, why

    monkeypatch.setattr(plan_module, "shape_snapshot", earlier)
    plan = _plan(runtime, intent)
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == code
    if code == "PLAN_INTENT_COVERAGE_GAP":
        assert "stock_as_of_unrealized" in {gap["kind"] for gap in plan["why"]["details"]["gaps"]}


@pytest.mark.parametrize("required", [[PLAN], [DAY, PLAN]])
def test_any_other_required_grouping_stays_a_hold(tmp_path: Path, required: list[str]) -> None:
    policy = {**DAY_POLICY, "required_group_by": required}
    runtime = _runtime(tmp_path / NS, policies=[policy], days_only=False)
    try:
        plan = _plan(runtime, "What's our MRR?")
    finally:
        runtime.close()
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "VALIDATION_FAILED"
    assert plan["why"]["errors"][0]["code"] == "POLICY_DENIED"
    assert PLAN not in plan["best"]["query_ir"].get("group_by", [])


def test_a_required_grouping_hidden_from_the_caller_is_never_added(tmp_path: Path) -> None:
    hidden = {"id": "policy.subscriptions.hide_names", "kind": "object_visibility",
              "object_ids": [NAME], "action": "hidden"}  # fmt: skip
    policy = {**DAY_POLICY, "required_group_by": [DAY, NAME]}
    runtime = _runtime(tmp_path / NS, policies=[policy, hidden], days_only=False)
    try:
        plan = _plan(runtime, "What's our MRR?")
    finally:
        runtime.close()
    assert plan["status"] == "low_confidence"
    assert plan["why"]["code"] == "VALIDATION_FAILED"
    assert "execute" not in plan["next"].get("ready_for", [])
    assert "account_name" not in str(plan)
