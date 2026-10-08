"""plan holds a balance draft that reads more than one day per answer row.

A stock answers with each series' last snapshot in each period, then adds up the series. When
a series stops reporting (an account closes and its daily rows stop), a period, or a read with
no time block, keeps that series' last value, so the draft is held unless each row reads one
day. plan reads a question that names no day on the last complete day, and one period on its
closing day (``planner/snapshot.py``); a series of periods stays held.
Accounts a, b and d hold 99 a day through 2026-10-04 on the basic plan; e holds 500 on
the pro plan through 2026-09-15 and f 50 through 2026-09-30. A stock keyed by its clock alone
(a daily rollup) is one series and is unchanged. Reference values come from plain SQL.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.expressions import ArithmeticExpr, LiteralExpr, MetricRecipeRefExpr
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.planner import plan_payload, time_checks
from semantic_rails.runtime import Runtime

NOW = {"now": "2026-10-05T06:00:00Z"}
SEED = """
CREATE TABLE account_day AS
SELECT account_id, CAST(day AS DATE) AS day, plan, mrr
FROM (VALUES ('a', 'basic', 99, DATE '2026-10-04'), ('b', 'basic', 99, DATE '2026-10-04'),
             ('d', 'basic', 99, DATE '2026-10-04'), ('e', 'pro', 500, DATE '2026-09-15'),
             ('f', 'pro', 50, DATE '2026-09-30')) AS t(account_id, plan, mrr, last_day),
     generate_series(DATE '2026-09-01', last_day, INTERVAL 1 DAY) AS s(day);
CREATE VIEW accounts AS SELECT DISTINCT account_id FROM account_day;
CREATE TABLE user_totals AS
SELECT CAST(day AS DATE) AS day, 10 * date_diff('day', DATE '2026-09-01', day) + 10 AS registered
FROM generate_series(DATE '2026-09-01', DATE '2026-10-04', INTERVAL 1 DAY) AS s(day);
"""
MEASURE = "measure.billing.mrr_all"
ROLE = "temporal_role.billing_account_day_day"
STOCK = "stock_as_of_unrealized"


def _package(root: Path, *, metric: bool, qualifying: bool = False) -> Path:
    def put(name: str, doc: dict[str, Any]) -> None:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    stock = {"kind": "stock", "snapshot": "end_of_period"}
    put("package.yml", {
        "schema_version": 1,
        "package": {"id": "billing", "namespace": "billing", "name": "billing",
                    "description": "Account balances", "warehouse": "duckdb",
                    "default_db": "billing.duckdb", "seed": {"kind": "external"}},
        "defaults": {"time": {"timezone": "UTC"}},
    })  # fmt: skip
    put("graph.yml", {"graph": {"entities": {
        "account_day": {"key": ["account_id", "day"], "model": "account_day"},
        "user_total": {"key": ["day"], "model": "user_totals"},
        **({"account": {"key": ["account_id"], "model": "accounts"}} if qualifying else {}),
    }}})  # fmt: skip
    put("models/account_day.yml", {"model": {
        "id": "account_day", "relation": "account_day",
        "entities": {"account_day": {}, **({"account": {}} if qualifying else {})},
        "times": {"day": {"column": "day", "kind": "date", "class": "as_of_time", "default": True}},
        "dimensions": {"plan": {"kind": "categorical", "domain": ["basic", "pro"]}},
        "measures": {"mrr_all": {
            "expr": "mrr", "accumulation": stock,
            **({"publish": False} if metric else {"label": "MRR"}),
        }, **({"account_count": {
            "label": "Account count", "kind": "entity_count", "entity_key": "account_id",
            "accumulation": {"kind": "event"}, "publish": False,
        }} if qualifying else {})},
    }})  # fmt: skip
    put("models/user_totals.yml", {"model": {
        "id": "user_totals", "relation": "user_totals", "entities": {"user_total": {}},
        "times": {"day": {"column": "day", "kind": "date", "class": "as_of_time", "default": True}},
        "measures": {"registered_users": {
            "label": "Registered users", "expr": "registered", "accumulation": stock,
        }},
    }})  # fmt: skip
    if metric:
        put("metrics/billing.yml", {"metrics": {"mrr": {
            "label": "MRR", "kind": "semi_additive",
            "temporal_role": ROLE,
            "expression": {"kind": "semi_additive", "measure": MEASURE},
        }}})  # fmt: skip
    if qualifying:
        put("models/accounts.yml", {"model": {
            "id": "accounts", "relation": "accounts", "entities": {"account": {}},
        }})  # fmt: skip
        put("metrics/qualifying.yml", {"metrics": {"qualifying_accounts": {
            "label": "Qualifying accounts", "kind": "aggregate", "temporal_role": ROLE,
            "expression": {
                "kind": "scoped_aggregate", "measure": "measure.billing.account_count",
                "aggregation": "count_distinct", "predicates": [{
                    "entity": "entity.billing_account", "measure": MEASURE,
                    "op": ">", "value": 0, "scope_mode": "entity_only",
                }],
            },
        }}})  # fmt: skip
    with duckdb.connect(str(root / "billing.duckdb")) as connection:
        connection.execute(SEED)
    return root


@pytest.fixture(scope="module", params=[False, True], ids=["measure", "metric"])
def runtime(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    root = _package(tmp_path_factory.mktemp("stock") / "billing", metric=request.param)
    engine = Runtime.from_path(str(root))
    try:
        engine._get_adapter()  # the module fixture owns its connection across tests
        yield engine
    finally:
        engine.close()


@pytest.fixture(scope="module", params=[False, True], ids=["measure", "metric"])
def qualifying_runtime(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    root = _package(
        tmp_path_factory.mktemp("qualifying") / "billing", metric=request.param, qualifying=True
    )
    engine = Runtime.from_path(str(root))
    try:
        engine._get_adapter()
        yield engine
    finally:
        engine.close()


def _plan(runtime: Runtime, intent: str, **kwargs: Any) -> dict[str, Any]:
    partial = {"policy_context": NOW, **kwargs.pop("partial", {})}
    return plan_payload(runtime, intent=intent, partial_query=partial, **kwargs)


def _reference(sql: str) -> int:
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        return int(connection.execute(sql).fetchone()[0])


def _closing_day(day: str) -> int:
    return _reference(f"SELECT SUM(mrr) FROM account_day WHERE day = DATE '{day}'")


def _stock_gaps(plan: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = (plan.get("why") or {}).get("details", {}).get("gaps", [])
    return [gap for gap in gaps if gap["kind"] == STOCK]


def _value(runtime: Runtime, plan: dict[str, Any]) -> int:
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query_ir"]
    [row] = runtime.query(query)["rows"]
    return int(row[query["select"][0]["as"]])


def _assert_held(plan: dict[str, Any], grain: str | None) -> None:
    assert plan["status"] == "low_confidence", plan.get("why")
    assert "execute" not in plan.get("next", {}).get("ready_for", [])
    [gap] = _stock_gaps(plan)
    assert gap["expected"] == {"grain": "day", "stocks": [MEASURE]}
    assert gap["actual"] == {"grain": grain}


@pytest.mark.parametrize("detail", ["best", "full"])
@pytest.mark.parametrize(
    ("intent", "grain"),
    [
        ("total MRR", None),
        ("MRR by week", "week"),
        ("MRR by month", "month"),
        ("MRR last 3 months", "month"),
    ],
)
def test_a_balance_over_more_than_one_day_per_row_is_held(
    runtime: Runtime, intent: str, grain: str | None, detail: str
) -> None:
    _assert_held(_plan(runtime, intent, detail=detail), grain)


@pytest.mark.parametrize("detail", ["best", "full"])
@pytest.mark.parametrize(
    ("intent", "day"),
    [
        ("What's our MRR?", "2026-10-04"),
        ("What is our MRR?", "2026-10-04"),
        ("MRR", "2026-10-04"),
        ("How much MRR do we have?", "2026-10-04"),
        ("MRR by plan", "2026-10-04"),
        ("MRR last week", "2026-10-04"),
        ("MRR last month", "2026-09-30"),
    ],
)
def test_a_balance_with_no_day_or_one_period_reads_its_closing_day(
    runtime: Runtime, intent: str, day: str, detail: str
) -> None:
    # No day: the last complete day before the clock. One period: its closing day.
    plan = _plan(runtime, intent, detail=detail)
    assert plan["best"]["query_ir"]["time"]["start"] == day
    assert _stock_gaps(plan) == []
    assert _value(runtime, plan) == _closing_day(day)


def test_a_balance_read_on_one_day_drops_closed_accounts(runtime: Runtime) -> None:
    # Each series' last value would still count the closed accounts e (500) and f (50).
    assert _closing_day("2026-10-04") == 297
    assert _reference("SELECT SUM(mrr) FROM account_day WHERE (account_id, day) IN "
                      "(SELECT (account_id, MAX(day)) FROM account_day GROUP BY account_id)") == 847  # fmt: skip
    assert _value(runtime, _plan(runtime, "MRR")) == 297


def test_mcp_plan_reads_a_balance_on_the_last_complete_day(runtime: Runtime) -> None:
    adapter = SemanticLayerMCPAdapter(runtime)
    plan = adapter.call_tool(
        "plan", {"intent": "What's our MRR?", "query": {"policy_context": NOW}}
    )
    assert plan["status"] == "ok", plan.get("why")
    time = plan["best"]["query_ir"]["time"]
    assert (time["grain"], time["start"], time["end"]) == ("day", "2026-10-04", "2026-10-05")
    held = adapter.call_tool("plan", {"intent": "MRR by week", "query": {"policy_context": NOW}})
    _assert_held(held, "week")


@pytest.mark.parametrize(
    ("intent", "day", "value"),
    [("MRR yesterday", "2026-10-04", 297), ("MRR on 2026-09-30", "2026-09-30", 347)],
)
def test_a_balance_on_one_day_is_unchanged(
    runtime: Runtime, intent: str, day: str, value: int
) -> None:
    plan = _plan(runtime, intent)
    assert plan["best"]["query_ir"]["time"]["grain"] == "day"
    assert _value(runtime, plan) == _closing_day(day) == value


def test_a_stock_keyed_by_its_clock_alone_is_unchanged(runtime: Runtime) -> None:
    plan = _plan(runtime, "registered users")
    assert "time" not in plan["best"]["query_ir"]
    reference = _reference("SELECT registered FROM user_totals ORDER BY day DESC LIMIT 1")
    assert reference == 340
    assert _value(runtime, plan) == reference


def test_a_caller_day_grain_is_the_callers_choice(runtime: Runtime) -> None:
    time = {"temporal_role": ROLE, "grain": "day", "start": "2026-10-04", "end": "2026-10-05"}
    plan = _plan(runtime, "MRR", partial={"time": time})
    assert _stock_gaps(plan) == []
    assert _value(runtime, plan) == _closing_day("2026-10-04")


def test_a_stock_read_through_a_metric_filter_or_a_nested_metric_is_held(
    runtime: Runtime,
) -> None:
    config = runtime._config
    users = {"expression": {"measure": "measure.billing.registered_users"}, "as": "users"}
    assert time_checks._stock_as_of_gaps(config, {"select": [users]}) == []
    mrr = {"measure": MEASURE, "aggregation": "last_value"}
    filtered = {"select": [users], "metric_filters": [{"expression": mrr, "op": ">", "value": 0}]}
    [gap] = time_checks._stock_as_of_gaps(config, filtered)
    assert gap.expected == {"grain": "day", "stocks": [MEASURE]}
    metric = next((row for row in config.metric_recipes if row.id == "metric.billing.mrr"), None)
    if metric is None:
        return
    doubled = replace(
        metric,
        id="metric.billing.mrr_doubled",
        expression=ArithmeticExpr("*", MetricRecipeRefExpr(metric.id), LiteralExpr(2)),
    )
    nested = replace(config, metric_recipes=[*config.metric_recipes, doubled])
    query = {"select": [{"expression": {"metric": doubled.id}, "as": "v"}]}
    [gap] = time_checks._stock_as_of_gaps(nested, query)
    assert gap.expected == {"grain": "day", "stocks": [MEASURE]}
    selected = {"select": [{"expression": {"metric": metric.id}, "as": "mrr"}]}
    _assert_held(_plan(runtime, "MRR by week", partial=selected), "week")


def test_a_where_item_with_a_stock_expression_is_refused(qualifying_runtime: Runtime) -> None:
    where = [
        {
            "field": "dimension.billing_account_day_plan",
            "op": "=",
            "value": "basic",
            "expression": {
                "kind": "metric_predicate",
                "entity": "entity.billing_account",
                "input": {"measure": MEASURE},
                "op": ">",
                "value": 0,
                "scope_mode": "entity_only",
            },
        }
    ]
    select = [{"expression": {"measure": "measure.billing.account_count"}, "as": "accounts"}]
    plan = _plan(qualifying_runtime, "Account count", partial={"select": select, "where": where})
    assert plan["status"] == "low_confidence"
    assert plan["best"]["validation_ok"] is False
    assert plan["best"]["query_ir"]["where"] == where
    assert plan["why"]["code"] == "VALIDATION_FAILED"
    report = qualifying_runtime.validate(plan["best"]["query_ir"])
    assert report["ok"] is False
    [error] = report["errors"]
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"] == "where[0]"
    assert error["details"]["unsupported_keys"] == ["expression"]
    assert "execute" not in plan.get("next", {}).get("ready_for", [])


@pytest.mark.parametrize("detail", ["best", "full"])
def test_an_outer_day_does_not_clear_a_stock_in_a_metric_predicate(
    qualifying_runtime: Runtime, detail: str
) -> None:
    plan = _plan(qualifying_runtime, "Qualifying accounts yesterday", detail=detail)
    assert plan["best"]["validation_ok"] is True, plan.get("why")
    assert plan["best"]["query_ir"]["time"]["grain"] == "day"
    assert plan["best"]["query_ir"]["select"][0]["expression"] == {
        "metric": "metric.billing.qualifying_accounts"
    }
    _assert_held(plan, "day")
    [hint] = [hint for hint in plan["why"]["recovery_hints"] if hint["kind"] == "ask_for_one_day"]
    assert hint["message"] == (
        "Choose a metric without a stock predicate, or select the balance directly."
    )


@pytest.mark.parametrize("route", ["metric_predicate", "scoped_aggregate"])
@pytest.mark.parametrize("direct_first", [False, True])
def test_a_shared_stock_is_marked_when_reached_through_a_predicate(
    runtime: Runtime, route: str, direct_first: bool
) -> None:
    config = runtime._config
    metric = next((row for row in config.metric_recipes if row.id == "metric.billing.mrr"), None)
    stock = {"metric": metric.id} if metric else {"measure": MEASURE}
    if metric:
        wrapper = replace(
            metric, id="metric.billing.nested_mrr", expression=MetricRecipeRefExpr(metric.id)
        )
        config = replace(config, metric_recipes=[*config.metric_recipes, wrapper])
        stock = {"metric": wrapper.id}
    predicate = {"entity": "entity.billing_account_day", "op": ">", "value": 0}
    expression = (
        {"kind": route, "input": stock, **predicate}
        if route == "metric_predicate"
        else {
            "kind": route,
            "measure": "measure.billing.registered_users",
            "predicates": [{**stock, **predicate}],
        }
    )
    select = [{"expression": stock}, {"expression": expression}]
    query = {"select": select if direct_first else select[::-1], "time": {"grain": "day"}}
    [gap] = time_checks._stock_as_of_gaps(config, query)
    assert gap.expected == {"grain": "day", "stocks": [MEASURE]}
    assert gap.actual == {"grain": "day"}


def test_the_recovery_hint_uses_the_selected_label_and_a_date_placeholder(runtime: Runtime) -> None:
    metric = next(
        (row for row in runtime._config.metric_recipes if row.id == "metric.billing.mrr"), None
    )
    partial = {"select": [{"expression": {"metric": metric.id}, "as": "mrr"}]} if metric else {}
    plan = _plan(runtime, "MRR by week", partial=partial)
    [hint] = [hint for hint in plan["why"]["recovery_hints"] if hint["kind"] == "ask_for_one_day"]
    assert hint["message"] == (
        "Ask for 'MRR yesterday' or 'MRR on <YYYY-MM-DD>', "
        "or set time.grain: day with that day's start and end."
    )


@pytest.mark.parametrize("intent", ["registered users", "registered users yesterday"])
def test_a_series_key_that_cannot_be_read_is_held(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, intent: str
) -> None:
    def unreadable(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("series key unreadable")

    monkeypatch.setattr(time_checks, "_snapshot_series_columns", unreadable)
    plan = _plan(runtime, intent)
    assert plan["status"] == "low_confidence", plan.get("why")
    assert "execute" not in plan.get("next", {}).get("ready_for", [])
    [gap] = _stock_gaps(plan)
    assert gap["expected"] == {"grain": "day", "stocks": []}


def test_a_direct_daily_stock_with_a_nonstock_predicate_is_unchanged(
    qualifying_runtime: Runtime,
) -> None:
    query = {
        "select": [
            {
                "expression": {
                    "kind": "scoped_aggregate",
                    "measure": MEASURE,
                    "predicates": [
                        {"measure": "measure.billing.account_count", "op": ">", "value": 0}
                    ],
                }
            }
        ],
        "time": {"grain": "day"},
    }
    assert time_checks._stock_as_of_gaps(qualifying_runtime._config, query) == []
