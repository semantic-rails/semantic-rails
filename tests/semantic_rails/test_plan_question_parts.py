"""A question that asks several things comes back as parts, each with its own draft and status.

A subscriptions package: accounts a, b and d are customers, c is internal. Signups, a closure and
an upgrade are events on the event clock; MRR is a daily balance on the account-day clock, so
"how many accounts signed up, and what was the MRR?" needs two queries. The policy variant reads
MRR per day (a metric constraint requires the day grouping). The clock is 2026-10-05T06:00Z: last
week is 2026-09-28..10-04 and this month is October. Every answer is checked against plain SQL on
the seed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from jsonschema import Draft202012Validator

import semantic_rails.cli.reports as reports
from semantic_rails.config_validation import PackageReference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import SemanticHTTPService, normalize_route
from semantic_rails.mcp import (
    MCP_SERVER_INSTRUCTIONS,
    SemanticLayerMCPAdapter,
    list_tool_definitions,
)
from semantic_rails.planner import plan as plan_module
from semantic_rails.planner import plan_payload
from semantic_rails.planner.question_parts import split_question
from semantic_rails.runtime import Runtime

NOW = {"now": "2026-10-05T06:00:00Z"}
NS = "subscriptions"
EVENT_CLOCK = f"temporal_role.{NS}_event_occurred_at"
DAY_CLOCK = f"temporal_role.{NS}_account_day_day"
CUSTOMERS = {"field": f"dimension.{NS}_account_segment", "op": "=", "value": "customer"}
SEED = """
CREATE TABLE accounts (account_id VARCHAR, name VARCHAR, segment VARCHAR);
INSERT INTO accounts VALUES ('a', 'Acme Data Co', 'customer'), ('b', 'Globex', 'customer'),
  ('c', 'QA Sandbox', 'internal'), ('d', 'Initech', 'customer');
CREATE TABLE events (event_id INTEGER, account_id VARCHAR, kind VARCHAR, occurred_at DATE);
INSERT INTO events VALUES (1, 'a', 'signup', '2026-09-02'), (2, 'b', 'signup', '2026-09-22'),
  (3, 'c', 'signup', '2026-09-29'), (4, 'd', 'signup', '2026-09-30'),
  (5, 'b', 'close', '2026-10-01'), (6, 'a', 'upgrade', '2026-10-02');
CREATE TABLE account_day (account_id VARCHAR, day DATE, plan VARCHAR, mrr DOUBLE);
INSERT INTO account_day SELECT a.account_id, CAST(d AS DATE),
  CASE WHEN a.account_id = 'a' AND d >= DATE '2026-10-02' THEN 'pro' ELSE 'basic' END,
  CASE WHEN a.account_id = 'b' AND d >= DATE '2026-10-01' THEN 0
       WHEN a.account_id = 'a' AND d >= DATE '2026-10-02' THEN 500 ELSE 99 END
  FROM accounts a, range(DATE '2026-09-01', DATE '2026-10-05', INTERVAL 1 DAY) t(d);
"""


def _events(kind: str, start: str, end: str) -> str:
    return (
        "SELECT COUNT(DISTINCT event_id) FROM events JOIN accounts USING (account_id) "
        f"WHERE segment = 'customer' AND kind = '{kind}' "
        f"AND occurred_at >= DATE '{start}' AND occurred_at < DATE '{end}'"
    )


def _balance(day: str) -> str:
    return (
        "SELECT SUM(mrr) FROM account_day JOIN accounts USING (account_id) "
        f"WHERE segment = 'customer' AND day = DATE '{day}'"
    )


def _package(root: Path, *, policy: bool) -> Path:
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
        "dimensions": {"kind": {"kind": "categorical", "domain": ["signup", "close", "upgrade"]}},
        "measures": {"events_all": {"kind": "entity_count", "entity_key": "event_id",
                                    "value_type": "count", "publish": False}},
    }})  # fmt: skip
    put("models/account_day.yml", {"model": {
        "id": "account_day", "relation": "account_day",
        "entities": {"account_day": {}, "account": {}},
        "times": {"day": {**day, **({"supported_grains": ["day"]} if policy else {})}},
        "dimensions": {"plan": {"kind": "categorical", "domain": ["basic", "pro"]}},
        "measures": {"mrr_all": {"expr": "mrr", "accumulation": stock, "publish": False}},
    }})  # fmt: skip
    metrics: dict[str, Any] = {
        key: {
            "label": label, "kind": "aggregate", "value_type": "count",
            "temporal_role": EVENT_CLOCK,
            "expression": {"kind": "aggregate", "measure": f"measure.{NS}.events_all",
                           "aggregation": "count_distinct", "filter": {"all": [
                               {"field": f"dimension.{NS}_event_kind", "op": "=", "value": kind},
                               CUSTOMERS]}},
        }
        for key, label, kind in [("new_accounts", "New accounts", "signup"),
                                 ("closures", "Closures", "close"),
                                 ("upgrades", "Upgrades", "upgrade")]
    }  # fmt: skip
    metrics["mrr"] = {
        "label": "MRR (USD)", "kind": "semi_additive", "temporal_role": DAY_CLOCK,
        "expression": {"kind": "semi_additive", "measure": f"measure.{NS}.mrr_all",
                       "filter": {"all": [CUSTOMERS]}},
    }  # fmt: skip
    put("metrics/accounts.yml", {"metrics": metrics})
    if policy:
        put("policies.yml", {"semantic_policies": [{
            "id": f"policy.{NS}.balance_per_day", "kind": "metric_constraint",
            "object_ids": [f"measure.{NS}.mrr_all"],
            "required_group_by": [f"dimension.{NS}_account_day_day"],
        }]})  # fmt: skip
    with duckdb.connect(str(root / "subscriptions.duckdb")) as connection:
        connection.execute(SEED)
    return root


@pytest.fixture(scope="module", params=["plain", "policy"])
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Runtime]:
    root = tmp_path_factory.mktemp(request.param) / NS
    engine = Runtime.from_path(str(_package(root, policy=request.param == "policy")))
    try:
        engine._get_adapter()  # the module fixture owns its connection across tests
        yield engine
    finally:
        engine.close()


def _plan(runtime: Runtime, intent: str, **kwargs: Any) -> dict[str, Any]:
    return plan_payload(runtime, intent=intent, partial_query={"policy_context": NOW}, **kwargs)


def _reference(sql: str) -> float:
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        return connection.execute(sql).fetchone()[0]


def _value(runtime: Runtime, query: dict[str, Any]) -> float:
    [item] = query["select"]
    [row] = runtime.query({**query, "policy_context": NOW})["rows"]
    return row[item["as"]]


# (question, [(metric, window start, window end, reference SQL)] per part).
ANSWERED = [
    (
        "Last week, how many new accounts were there, and what was the MRR?",
        [
            ("new_accounts", "2026-09-28", "2026-10-05", _events("signup", "2026-09-28", "2026-10-05")),
            ("mrr", "2026-10-04", "2026-10-05", _balance("2026-10-04")),
        ],
    ),
    (
        "How many new accounts last week, and how many closures this month?",
        [
            ("new_accounts", "2026-09-28", "2026-10-05", _events("signup", "2026-09-28", "2026-10-05")),
            ("closures", "2026-10-01", "2026-11-01", _events("close", "2026-10-01", "2026-11-01")),
        ],
    ),
]  # fmt: skip


@pytest.mark.parametrize(("question", "expected"), ANSWERED)
def test_each_part_answers_its_clause_like_its_reference(
    runtime: Runtime, question: str, expected: list[tuple[str, str, str, str]]
) -> None:
    payload = _plan(runtime, question, detail="best")

    assert payload["status"] == "ok", payload.get("why")
    assert payload["next"]["ready_for"] == ["execute"]
    assert "why" not in payload and "warnings" not in payload
    assert len(payload["parts"]) == len(expected)
    for part, (metric, start, end, sql) in zip(payload["parts"], expected, strict=True):
        assert part["status"] == "ok", part.get("why")
        query = part["best"]["query_ir"]
        assert query["version"] == 1
        assert [item["expression"] for item in query["select"]] == [
            {"metric": f"metric.{NS}.{metric}"}
        ]
        assert (query["time"]["start"], query["time"]["end"]) == (start, end)
        assert _value(runtime, query) == _reference(sql)
        # Each part is its own words plus the shared leading phrase.
        assert part["text"] == "".join(question[low:high] for low, high in part["spans"])
    # A client that reads only best gets the first part.
    assert payload["best"] == payload["parts"][0]["best"]


def test_the_references_are_the_documented_answers() -> None:
    assert [_reference(sql) for _metric, _start, _end, sql in ANSWERED[0][1]] == [1, 599]
    assert [_reference(sql) for _metric, _start, _end, sql in ANSWERED[1][1]] == [1, 1]


def test_the_query_detail_returns_every_parts_query(runtime: Runtime) -> None:
    question = ANSWERED[0][0]
    best = _plan(runtime, question, detail="best")
    compact = _plan(runtime, question, detail="query")

    assert compact["status"] == "ok"
    assert [part["best"]["query_ir"] for part in compact["parts"]] == [
        part["best"]["query_ir"] for part in best["parts"]
    ]
    assert compact["best"]["query_ir"] == compact["parts"][0]["best"]["query_ir"]
    assert {key for part in compact["parts"] for key in part} <= {
        "text", "spans", "status", "best", "why", "assumptions", "warnings", "tie_break_hints",
    }  # fmt: skip


def test_mcp_and_rest_plan_the_same_parts(runtime: Runtime) -> None:
    question = ANSWERED[1][0]
    mcp = SemanticLayerMCPAdapter(runtime).call_tool(
        "plan", {"intent": question, "detail": "best", "query": {"policy_context": NOW}}
    )
    rest, code = SemanticHTTPService(runtime).handle(
        "POST",
        normalize_route("/api/v1/plan"),
        {"intent": question, "detail": "best", "policy_context": NOW},
    )

    assert code == 200
    assert mcp["status"] == rest["status"] == "ok"
    assert [(part["status"], part["best"]["query_ir"]) for part in mcp["parts"]] == [
        (part["status"], part["best"]["query_ir"]) for part in rest["parts"]
    ]
    # The MCP plan tool declares parts, and its server instructions say to run each one.
    [tool] = [tool for tool in list_tool_definitions() if tool["name"] == "plan"]
    assert "parts" in tool["outputSchema"]["properties"]
    Draft202012Validator(tool["outputSchema"]).validate(mcp)
    assert (
        "When status is ok, a question asking several things returns parts; execute each "
        "part's best.query_ir." in MCP_SERVER_INSTRUCTIONS
    )


@pytest.mark.parametrize(
    "question",
    [
        # One question, or a list the conjoined pattern answers as one query.
        "How many new accounts were there last week?",
        "New accounts and closures last week",
    ],
)
def test_a_question_one_plan_answers_has_no_parts(runtime: Runtime, question: str) -> None:
    payload = _plan(runtime, question)

    assert payload["status"] == "ok", payload.get("why")
    assert "parts" not in payload


@pytest.mark.parametrize(
    "question",
    [
        "Revenue by plan and region",
        "Revenue by plan and which region",
        'What is "orders, and what revenue"?',
        "How many new accounts and closures last week?",
        "Last week, what was the MRR?",
    ],
)
def test_a_grouping_list_quote_or_conjoined_subject_is_not_split(
    runtime: Runtime, question: str
) -> None:
    assert split_question(question, runtime._config) is None


# (question, hold, the parts it names).
HELD = [
    (
        "Last week, how many new accounts were there, and what share of those closed?",
        "dependent_part",
        [2],
    ),
    ("Last week, what was the MRR, and how much of that came from new accounts?", "dependent_part", [2]),
    ("How many new accounts last week, and how many?", "part_without_subject", [2]),
    (
        "How many new accounts, how many closures, how many upgrades, what was the MRR, and "
        "how many accounts?",
        "too_many_parts",
        [1, 2, 3, 4, 5],
    ),
    # A trailing window, grouping or filter may be meant for every part.
    ("How many new accounts, and how many closures last week?", "part_without_window", [1]),
    ("How many new accounts, and how many closures by segment?", "part_without_grouping", [1]),
    # Every grouping form plan reads, not only "by".
    ("How many new accounts, and how many closures per segment?", "part_without_grouping", [1]),
    ("How many new accounts, and how many closures for each segment?", "part_without_grouping", [1]),
    ("How many new accounts, and how many closures monthly?", "part_without_grouping", [1]),
    ("How many new accounts, and how many closures over time?", "part_without_grouping", [1]),
    (
        "Last week, how many new accounts were there, and how many closures where segment is "
        "internal?",
        "part_filters_differ",
        [1, 2],
    ),
]


@pytest.mark.parametrize(("question", "hold", "numbers"), HELD)
def test_parts_plan_cant_read_alone_are_held(
    runtime: Runtime, question: str, hold: str, numbers: list[int]
) -> None:
    payload = _plan(runtime, question, detail="best")

    assert payload["status"] == "low_confidence"
    assert "ready_for" not in payload["next"]
    assert payload["why"]["code"] == "PLAN_PARTS_HELD"
    assert payload["why"]["details"]["reason"] == hold
    assert [part["part"] for part in payload["why"]["details"]["parts"]] == numbers
    # The parts are listed, unplanned.
    assert payload["parts"] and all(set(part) == {"text", "spans"} for part in payload["parts"])


def test_a_part_not_ready_holds_the_whole_with_its_status(runtime: Runtime) -> None:
    payload = _plan(runtime, "How many new accounts last week, and what was the weather last week?")

    assert payload["status"] != "ok"
    assert "ready_for" not in payload["next"]
    first, second = payload["parts"]
    assert first["status"] == "ok"
    assert payload["status"] == second["status"] != "ok"
    assert payload["why"]["code"] == "PLAN_PARTS_NOT_READY"
    assert payload["why"]["details"]["parts"] == [
        {
            "part": 2,
            "text": second["text"],
            "status": second["status"],
            "code": second["why"]["code"],
        }
    ]


def test_the_whole_is_ready_only_when_every_part_is(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forcing one part's plan to be held holds the whole, whatever the other parts say."""

    question = ANSWERED[0][0]
    planned = plan_module._question_payload

    def second_part_held(runtime: Any, intent: str, *args: Any) -> dict[str, Any]:
        payload = planned(runtime, intent, *args)
        if intent.endswith("MRR"):
            payload = {
                **payload,
                "status": "low_confidence",
                "why": {"code": "FORCED", "message": "x"},
            }
        return payload

    monkeypatch.setattr(plan_module, "_question_payload", second_part_held)
    payload = _plan(runtime, question)

    assert [part["status"] for part in payload["parts"]] == ["ok", "low_confidence"]
    assert payload["status"] == "low_confidence"
    assert "ready_for" not in payload["next"]
    assert payload["why"]["details"]["parts"][0]["code"] == "FORCED"


def test_a_part_warning_is_the_whole_payloads(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    planned = plan_module._question_payload
    warning = {"code": "FORCED", "severity": "warning", "message": "x"}

    def warned(runtime: Any, intent: str, *args: Any) -> dict[str, Any]:
        payload = planned(runtime, intent, *args)
        return {**payload, "warnings": [warning]} if intent.endswith("MRR") else payload

    monkeypatch.setattr(plan_module, "_question_payload", warned)
    payload = _plan(runtime, ANSWERED[0][0])

    assert payload["warnings"] == [warning]


def test_a_part_the_engine_refuses_keeps_the_whole_questions_hold(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    question = ANSWERED[0][0]
    planned = plan_module._question_payload
    whole = planned(runtime, question, {"policy_context": NOW}, "best", 3, runtime._config)

    def refused(runtime: Any, intent: str, *args: Any) -> dict[str, Any]:
        if intent.endswith("MRR"):
            raise SemanticLayerError("INVALID_QUERY", "refused")
        return planned(runtime, intent, *args)

    monkeypatch.setattr(plan_module, "_question_payload", refused)
    payload = _plan(runtime, question, detail="best")

    assert "parts" not in payload
    assert (payload["status"], payload["why"]) == (whole["status"], whole["why"])
    assert whole["status"] != "ok"


def test_ask_never_runs_one_part_as_the_whole_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _package(tmp_path / NS, policy=False)
    planned = reports.plan_payload

    def at_now(runtime: Any, **kwargs: Any) -> dict[str, Any]:
        return planned(runtime, **{**kwargs, "partial_query": {"policy_context": NOW}})

    monkeypatch.setattr(reports, "plan_payload", at_now)
    report = reports.ask_report(
        PackageReference(source_path=str(root)), question=ANSWERED[0][0], execute=True
    )

    assert report["plan"] and report["ok"] is False
    assert [error["code"] for error in report["errors"]] == ["PLAN_PARTS"]
    assert report["errors"][0]["details"]["parts"] == [
        "Last week, how many new accounts were there",
        "Last week, what was the MRR",
    ]
    assert "result" not in report


def test_a_caller_query_is_never_split_across_parts(runtime: Runtime) -> None:
    question = ANSWERED[0][0]
    select = [{"expression": {"metric": f"metric.{NS}.new_accounts"}}]
    payload = plan_payload(
        runtime, intent=question, partial_query={"policy_context": NOW, "select": select}
    )

    assert "parts" not in payload
    assert payload["status"] != "ok"
