"""plan selects the subject the package's own names point to.

The neutral package counts accounts' events and calls, and each metric keeps the customer
accounts. A metric's whole synonym selects it, even beside a measure whose short name is a
word of the metric's own names. A draft over a building-block measure answers with the one
metric that is its governed form, the filtered aggregate bare or inside ``COALESCE(..., 0)``,
whichever path drafted it. Gold values come from plain SQL over the seed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.planner import generators, plan_payload
from semantic_rails.planner import plan as planner
from semantic_rails.planner._base import _named_metric
from semantic_rails.runtime import Runtime
from tests.semantic_rails.result_helpers import disable_planner_patterns

NOW = {"now": "2026-10-05T06:00:00Z"}  # "last week" is 2026-09-28 .. 2026-10-04
SEED = """
CREATE TABLE accounts (account_id VARCHAR, name VARCHAR, segment VARCHAR);
INSERT INTO accounts VALUES ('a','Acme Data Co','customer'),('b','Globex','customer'),
  ('c','QA Sandbox','internal'),('d','Initech','customer');
CREATE TABLE events (event_id INTEGER, account_id VARCHAR, kind VARCHAR, occurred_at DATE);
INSERT INTO events VALUES (1,'a','signup','2026-09-02'),(2,'b','signup','2026-09-22'),
  (3,'c','signup','2026-09-29'),(4,'d','signup','2026-09-30'),(5,'b','close','2026-10-01'),
  (6,'a','upgrade','2026-10-02');
CREATE TABLE account_day (account_id VARCHAR, day DATE, plan VARCHAR, mrr DOUBLE);
INSERT INTO account_day SELECT a.account_id, d::DATE,
  CASE WHEN a.account_id='a' AND d >= DATE '2026-10-02' THEN 'pro' ELSE 'basic' END,
  CASE WHEN a.account_id='b' AND d >= DATE '2026-10-01' THEN 0
       WHEN a.account_id='a' AND d >= DATE '2026-10-02' THEN 500 ELSE 99 END
  FROM accounts a, range(DATE '2026-09-01', DATE '2026-10-05', INTERVAL 1 DAY) t(d);
CREATE TABLE calls (call_id INTEGER, account_id VARCHAR, called_at DATE, ended_at DATE);
INSERT INTO calls VALUES (1,'a','2026-09-29','2026-09-29'),(2,'b','2026-09-30','2026-10-06'),
  (3,'c','2026-10-01','2026-10-01'),(4,'c','2026-10-02','2026-10-02'),
  (5,'d','2026-09-15','2026-09-15'),(6,'a','2026-10-04','2026-10-04'),
  (7,'c','2026-09-10','2026-09-10');
"""
EVENTS_ALL = "measure.subscriptions.events_all"
ACCOUNTS_ALL = "measure.subscriptions.accounts_all"
CALLS_ALL = "measure.subscriptions.calls_all"
CALLS = "metric.subscriptions.calls"
MRR_ALL = "measure.subscriptions.mrr_all"
UPGRADES = "metric.subscriptions.upgraded_accounts"
SEGMENT = "dimension.subscriptions_account_segment"
EVENT_CLOCK = "temporal_role.subscriptions_event_occurred_at"
DAY_CLOCK = "temporal_role.subscriptions_account_day_day"
CALL_CLOCK = "temporal_role.subscriptions_call_called_at"
END_CLOCK = "temporal_role.subscriptions_call_ended_at"
LAST_WEEK = "{} >= DATE '2026-09-28' AND {} < DATE '2026-10-05'"
UPGRADED = "How many accounts moved to a bigger plan last week?"


def _package(
    root: Path,
    *,
    calls: str = "zero_filled",
    clock: str = CALL_CLOCK,
    governors: tuple[str, ...] = ("calls",),
    published_calls: bool = False,
    customer_mrr: bool = False,
) -> Path:
    """Accounts with their events, daily balances and calls. ``calls`` is the form of the
    governed call metrics: ``zero_filled`` (``COALESCE(<filtered count>, 0)``), ``bare`` or
    ``one_filled``. ``published_calls`` publishes the calls measure; ``customer_mrr`` replaces
    MRR with one zero-filled metric of the customers' balances."""

    customer = {"field": SEGMENT, "op": "=", "value": "customer"}

    def counted(measure: str, *conditions: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "aggregate", "measure": measure, "aggregation": "count_distinct",
                "filter": {"all": [*conditions, customer]}}  # fmt: skip

    def event_kind(kind: str) -> dict[str, Any]:
        return {"field": "dimension.subscriptions_event_kind", "op": "=", "value": kind}

    metrics: dict[str, Any] = {
        key: {"label": label, "kind": "aggregate", "value_type": "count",
              "temporal_role": EVENT_CLOCK, "expression": counted(EVENTS_ALL, event_kind(kind))}
        for key, label, kind in [("new_accounts", "New accounts", "signup"),
                                 ("closures", "Closures", "close"),
                                 ("upgraded_accounts", "Accounts that upgraded (customers)", "upgrade")]
    }  # fmt: skip
    metrics["upgraded_accounts"]["synonyms"] = ["moved to a bigger plan"]
    metrics["net_new_accounts"] = {
        "label": "Net new accounts (customers)", "kind": "derived", "value_type": "count",
        "temporal_role": EVENT_CLOCK,
        "expression": {"kind": "arithmetic", "op": "subtract",
                       "left": {"kind": "metric", "metric": "metric.subscriptions.new_accounts"},
                       "right": {"kind": "metric", "metric": "metric.subscriptions.closures"}},
    }  # fmt: skip
    metrics["mrr"] = {
        "label": "MRR (USD)", "kind": "semi_additive",
        "temporal_role": "temporal_role.subscriptions_account_day_day",
        "expression": {"kind": "semi_additive", "measure": "measure.subscriptions.mrr_all"},
    }  # fmt: skip
    if customer_mrr:
        del metrics["mrr"]
        balances = {"kind": "aggregate", "measure": MRR_ALL, "aggregation": "last_value",
                    "filter": {"all": [customer]}}  # fmt: skip
        metrics["customer_mrr"] = {
            "label": "Customer MRR", "kind": "derived", "value_type": "currency",
            "temporal_role": DAY_CLOCK,
            "expression": {"kind": "call", "name": "COALESCE",
                           "args": [balances, {"kind": "literal", "value": 0}]},
        }  # fmt: skip
    filtered = counted(CALLS_ALL)
    filled = {"zero_filled": 0, "one_filled": 1}
    labels = {"calls": "Calls", "customer_calls": "Customer calls", "paid_calls": "Paid calls",
              "call": "Customer calls"}  # fmt: skip
    for key in governors:
        metrics[key] = {
            "label": labels[key], "kind": "aggregate" if calls == "bare" else "derived",
            "value_type": "count", "temporal_role": clock,
            "expression": filtered if calls == "bare" else {
                "kind": "call", "name": "COALESCE",
                "args": [filtered, {"kind": "literal", "value": filled[calls]}]},
        }  # fmt: skip
    files = {
        "package.yml": {
            "schema_version": 1,
            "package": {"id": "subscriptions", "namespace": "subscriptions",
                        "name": "Subscriptions", "description": "Account activity",
                        "warehouse": "duckdb", "default_db": "subscriptions.duckdb",
                        "seed": {"kind": "external"}},
            "defaults": {"time": {"timezone": "UTC"}},
        },
        "graph.yml": {"graph": {
            "entities": {"account": {"key": ["account_id"], "model": "accounts"},
                         "event": {"key": ["event_id"], "model": "events"},
                         "account_day": {"key": ["account_id", "day"], "model": "account_day"},
                         "call": {"key": ["call_id"], "model": "calls"}},
            "relationships": {
                "event_account": {"entities": ["event", "account"], "cardinality": "many_to_one"},
                "day_account": {"entities": ["account_day", "account"],
                                "cardinality": "many_to_one"},
                "call_account": {"entities": ["call", "account"], "cardinality": "many_to_one"},
            },
        }},
        "models/accounts.yml": {"model": {
            "id": "accounts", "relation": "accounts", "entities": {"account": {}},
            "dimensions": {"name": {"kind": "categorical"},
                           "segment": {"kind": "categorical", "domain": ["customer", "internal"]}},
            "measures": {
                "accounts_all": {"label": "Accounts (all segments)", "kind": "entity_count",
                                 "entity_key": "account_id", "value_type": "count"},
                "workspaces_all": {"label": "Workspaces (all segments)", "kind": "entity_count",
                                   "entity_key": "account_id", "value_type": "count"},
            },
        }},
        "models/events.yml": {"model": {
            "id": "events", "relation": "events",
            "entities": {"event": {}, "account": {"column": "account_id"}},
            "times": {"occurred_at": {"column": "occurred_at", "kind": "date",
                                      "class": "event_time", "default": True}},
            "dimensions": {"kind": {"kind": "categorical",
                                    "domain": ["signup", "close", "upgrade"]}},
            "measures": {"events_all": {"kind": "entity_count", "entity_key": "event_id",
                                        "value_type": "count", "publish": False}},
        }},
        "models/account_day.yml": {"model": {
            "id": "account_day", "relation": "account_day",
            "entities": {"account_day": {}, "account": {"column": "account_id"}},
            "times": {"day": {"column": "day", "kind": "date", "class": "as_of_time",
                              "default": True}},
            "dimensions": {"plan": {"kind": "categorical", "domain": ["basic", "pro"]}},
            "measures": {"mrr_all": {"expr": "mrr", "publish": False,
                                     "accumulation": {"kind": "stock",
                                                      "snapshot": "end_of_period"}}},
        }},
        "models/calls.yml": {"model": {
            "id": "calls", "relation": "calls",
            "entities": {"call": {}, "account": {"column": "account_id"}},
            "times": {"called_at": {"column": "called_at", "kind": "date", "class": "event_time",
                                    "default": True},
                      "ended_at": {"column": "ended_at", "kind": "date", "class": "event_time"}},
            "measures": {"calls_all": {"label": "Calls (all segments)", "kind": "entity_count",
                                       "entity_key": "call_id", "value_type": "count",
                                       "publish": published_calls},
                         **({"callers": {"label": "Callers", "kind": "entity_count",
                                         "entity_key": "account_id", "value_type": "count"}}
                            if published_calls else {})},
        }},
        "metrics/accounts.yml": {"metrics": metrics},
    }  # fmt: skip
    for name, doc in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    with duckdb.connect(str(root / "subscriptions.duckdb")) as connection:
        connection.execute(SEED)
    return root


@pytest.fixture(scope="module")
def engines(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Runtime]]:
    """One runtime per governed-metric form (see ``_package``)."""

    built: dict[str, Runtime] = {}
    try:
        for form in ("zero_filled", "bare", "one_filled"):
            root = _package(tmp_path_factory.mktemp(form) / "subscriptions", calls=form)
            engine = built[form] = Runtime.from_path(str(root))
            engine._get_adapter()  # the module fixture owns its connection across tests
        yield built
    finally:
        for engine in built.values():
            engine.close()


def _gold(sql: str) -> int:
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        return int(connection.execute(sql).fetchone()[0])


def _calls_gold(*, customers: bool, week: bool) -> int:
    where = [
        *(["segment = 'customer'"] if customers else []),
        *([LAST_WEEK.format("called_at", "called_at")] if week else []),
    ]
    return _gold(
        "SELECT COUNT(DISTINCT call_id) FROM calls JOIN accounts USING (account_id)"
        + (f" WHERE {' AND '.join(where)}" if where else "")
    )


def _plan(engine: Runtime, intent: str, **partial: Any) -> dict[str, Any]:
    return plan_payload(engine, intent=intent, partial_query={"policy_context": NOW, **partial})


def _value(engine: Runtime, query: dict[str, Any]) -> int:
    rows = engine.query({**query, "policy_context": NOW})["rows"]
    assert len(rows) == 1, rows
    return int(rows[0][query["select"][0]["as"]])


def _selected(plan: dict[str, Any]) -> dict[str, Any]:
    expression = plan["best"]["query_ir"]["select"][0]["expression"]
    return {key: value for key, value in expression.items() if key != "aggregation"}


def _governed_gaps(plan: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = plan.get("why", {}).get("details", {}).get("gaps", [])
    return [gap for gap in gaps if gap["kind"] == "governed_metric_unrealized"]


def _assert_ok(plan: dict[str, Any]) -> None:
    assert plan["status"] == "ok", plan.get("why")
    assert "execute" in plan["next"]["ready_for"]


def _assert_held(plan: dict[str, Any], status: str = "low_confidence") -> None:
    assert plan["status"] == status, plan.get("why")
    assert "execute" not in plan.get("next", {}).get("ready_for", [])


def test_the_reference_counts() -> None:
    assert _calls_gold(customers=False, week=True) == 5
    assert _calls_gold(customers=True, week=True) == 3
    assert _calls_gold(customers=True, week=False) == 4
    upgraded = (
        "SELECT COUNT(DISTINCT event_id) FROM events JOIN accounts USING (account_id) "
        f"WHERE kind = 'upgrade' AND segment = 'customer' AND {LAST_WEEK}"
    )
    assert _gold(upgraded.format("occurred_at", "occurred_at")) == 1


def test_a_whole_synonym_selects_its_metric_beside_a_measure_named_by_its_own_words(
    engines: dict[str, Runtime],
) -> None:
    """Held before: "Accounts (all segments)" read as "accounts" vetoed the synonym, though
    "accounts" is a word of the metric's own label."""

    engine = engines["zero_filled"]
    named = _named_metric(engine._config, UPGRADED)
    assert named is not None and named[0].id == UPGRADES
    plan = _plan(engine, UPGRADED)
    _assert_ok(plan)
    assert _selected(plan) == {"metric": UPGRADES}
    assert _value(engine, plan["best"]["query_ir"]) == 1


def test_a_measure_name_outside_the_metric_s_words_still_vetoes(
    engines: dict[str, Runtime],
) -> None:
    """Unchanged: "workspaces" is no word of the metric's names, so the measure it names
    still vetoes the synonym, and the draft is held as before."""

    engine = engines["zero_filled"]
    question = "How many workspaces moved to a bigger plan last week?"
    assert _named_metric(engine._config, question) is None
    plan = _plan(engine, question)
    _assert_held(plan)
    assert _selected(plan) == {"measure": "measure.subscriptions.workspaces_all"}


@pytest.mark.parametrize("form", ["zero_filled", "bare"])
@pytest.mark.parametrize(
    ("intent", "week"), [("How many calls last week?", True), ("How many calls?", False)]
)
def test_a_building_block_is_answered_with_its_governed_form(
    engines: dict[str, Runtime], form: str, intent: str, week: bool
) -> None:
    """Held before (``governed_metric_unrealized`` naming the metric): a zero-filled metric
    was never swapped in, and no draft without time words was."""

    engine = engines[form]
    plan = _plan(engine, intent)
    _assert_ok(plan)
    query = plan["best"]["query_ir"]
    assert _selected(plan) == {"metric": CALLS}
    assert ("time" in query) is week
    assert plan["best"]["resolved"][0]["id"] == CALLS
    assert _value(engine, query) == _calls_gold(customers=True, week=week)


def test_a_catalog_fallback_draft_over_the_measure_takes_the_same_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Held before. Discover never offers a building block, so the fallback is made to choose
    the measure here: whichever path drafts it, the draft answers with the metric."""

    engine = Runtime.from_path(str(_package(tmp_path / "subscriptions")))
    try:
        disable_planner_patterns(engine, "metric_by_dimension_rollup")
        choice = {"id": CALLS_ALL, "kind": "measure", "label": "Calls (all segments)"}
        monkeypatch.setattr(generators, "_choose_object_for_terms", lambda *_, **__: choice)
        plan = _plan(engine, "How many calls last week?")
        _assert_ok(plan)
        assert plan["best"]["pattern"] == "catalog_fallback"
        assert plan["best"]["query_ir"]["select"] == [
            {"as": "calls", "expression": {"metric": CALLS}}
        ]
        assert plan["best"]["resolved"][0]["id"] == CALLS
        assert _value(engine, plan["best"]["query_ir"]) == _calls_gold(customers=True, week=True)
    finally:
        engine.close()


def _assert_measure_held(plan: dict[str, Any], metrics: list[str]) -> None:
    _assert_held(plan)
    assert _selected(plan) == {"measure": CALLS_ALL}
    assert [gap["expected"]["metrics"] for gap in _governed_gaps(plan)] == [metrics]


@pytest.mark.parametrize("intent", ["How many calls last week?", "How many calls?"])
def test_a_draft_that_misses_the_swap_is_still_held(
    engines: dict[str, Runtime], monkeypatch: pytest.MonkeyPatch, intent: str
) -> None:
    """The swap only moves drafts that readiness refuses without it."""

    monkeypatch.setattr(planner, "_governed_target", lambda *_: None)
    _assert_measure_held(_plan(engines["zero_filled"], intent), [CALLS])


# The holds below are unchanged.


def test_a_filler_other_than_zero_is_not_a_governed_form(engines: dict[str, Runtime]) -> None:
    engine = engines["one_filled"]
    plan = _plan(engine, "How many calls last week?")
    _assert_measure_held(plan, [CALLS])
    assert _value(engine, plan["best"]["query_ir"]) == _calls_gold(customers=False, week=True)


def test_a_filter_on_the_narrowing_dimension_keeps_the_measure(
    engines: dict[str, Runtime],
) -> None:
    engine = engines["zero_filled"]
    plan = _plan(engine, "How many calls from internal accounts last week?")
    _assert_measure_held(plan, [CALLS])
    assert plan["best"]["query_ir"]["where"] == [{"field": SEGMENT, "op": "=", "value": "internal"}]


def test_a_caller_naming_the_measure_keeps_it(engines: dict[str, Runtime]) -> None:
    engine = engines["zero_filled"]
    select = [{"as": "calls_all", "expression": {"measure": CALLS_ALL}}]
    plan = _plan(engine, "How many calls last week?", select=select)
    _assert_ok(plan)
    assert _selected(plan) == {"measure": CALLS_ALL}
    assert _value(engine, plan["best"]["query_ir"]) == _calls_gold(customers=False, week=True)


def test_a_governed_form_on_another_clock_is_not_swapped_in(tmp_path: Path) -> None:
    engine = Runtime.from_path(str(_package(tmp_path / "subscriptions", clock=END_CLOCK)))
    try:
        plan = _plan(engine, "How many calls last week?")
        _assert_measure_held(plan, [CALLS])
        assert plan["best"]["query_ir"]["time"]["temporal_role"] == CALL_CLOCK
    finally:
        engine.close()


def test_two_governors_that_fit_equally_swap_neither(tmp_path: Path) -> None:
    """The question names neither metric, and the building block has two."""

    governors = ("customer_calls", "paid_calls")
    engine = Runtime.from_path(str(_package(tmp_path / "subscriptions", governors=governors)))
    try:
        plan = _plan(engine, "How many calls last week?")
        _assert_measure_held(plan, [f"metric.subscriptions.{key}" for key in governors])
    finally:
        engine.close()
