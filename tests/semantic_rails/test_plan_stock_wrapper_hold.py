"""A metric that reads a balance through a wrapper is answered only for complete days.

A billing package: customers a and b and internal account c each have a daily MRR row from
2026-09-01 to 2026-10-04, except that neither customer has a row on 2026-09-15. "Customer MRR"
is the customers' balance zero-filled (``COALESCE(<filtered last value>, 0)``), "Customer ARR"
is twelve times that metric, and "Customer balance" is a scoped aggregate of the customers'
rows. None is one plain aggregate, so plan doesn't shape them to a read day; a draft over any of
them that reads a day not yet complete is held as a plain balance's is. The clock is
2026-10-05T06:00Z, so the last complete day is 2026-10-04. Every answer is checked against
plain SQL on the seed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime

NOW = {"now": "2026-10-05T06:00:00Z"}
NS = "billing"
CLOCK = f"temporal_role.{NS}_account_day_day"
MRR_ALL = f"measure.{NS}.mrr_all"
# Each metric, and its value as a multiple of the customers' balance.
METRICS = {
    "Customer MRR": (f"metric.{NS}.customer_mrr", 1),
    "Customer ARR": (f"metric.{NS}.customer_arr", 12),
    "Customer balance": (f"metric.{NS}.customer_balance", 1),
}
SEED = """
CREATE TABLE accounts (account_id VARCHAR, segment VARCHAR);
INSERT INTO accounts VALUES ('a', 'customer'), ('b', 'customer'), ('c', 'internal');
CREATE TABLE account_day AS
SELECT a.account_id, CAST(d AS DATE) AS day,
  CAST(CASE WHEN a.account_id = 'a' AND d >= DATE '2026-10-01' THEN 500 ELSE 99 END AS DOUBLE)
    AS mrr
FROM accounts a, range(DATE '2026-09-01', DATE '2026-10-05', INTERVAL 1 DAY) t(d)
WHERE a.segment = 'internal' OR d <> DATE '2026-09-15';
"""


def _package(root: Path) -> Path:
    def put(name: str, doc: dict[str, Any]) -> None:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    customers = {"field": f"dimension.{NS}_account_segment", "op": "=", "value": "customer"}
    balance = {"kind": "aggregate", "measure": MRR_ALL, "aggregation": "last_value",
               "filter": {"all": [customers]}}  # fmt: skip
    put("package.yml", {
        "schema_version": 1,
        "package": {"id": NS, "namespace": NS, "name": "Billing",
                    "description": "Account balances", "warehouse": "duckdb",
                    "default_db": "billing.duckdb", "seed": {"kind": "external"}},
        "defaults": {"time": {"timezone": "UTC"}},
    })  # fmt: skip
    put("graph.yml", {"graph": {"entities": {
        "account": {"key": ["account_id"], "model": "accounts"},
        "account_day": {"key": ["account_id", "day"], "model": "account_day"},
    }}})  # fmt: skip
    put("models/accounts.yml", {"model": {
        "id": "accounts", "relation": "accounts", "entities": {"account": {}},
        "dimensions": {"segment": {"kind": "categorical", "domain": ["customer", "internal"]}},
    }})  # fmt: skip
    put("models/account_day.yml", {"model": {
        "id": "account_day", "relation": "account_day",
        "entities": {"account_day": {}, "account": {}},
        "times": {"day": {"column": "day", "kind": "date", "class": "as_of_time",
                          "default": True}},
        "measures": {"mrr_all": {"expr": "mrr", "publish": False,
                                 "accumulation": {"kind": "stock", "snapshot": "end_of_period"}}},
    }})  # fmt: skip
    put("metrics/billing.yml", {"metrics": {
        "customer_mrr": {
            "label": "Customer MRR", "kind": "derived", "value_type": "currency",
            "temporal_role": CLOCK,
            "expression": {"kind": "call", "name": "COALESCE",
                           "args": [balance, {"kind": "literal", "value": 0}]},
        },
        "customer_arr": {
            "label": "Customer ARR", "kind": "derived", "value_type": "currency",
            "temporal_role": CLOCK,
            "expression": {"kind": "arithmetic", "op": "multiply",
                           "left": {"kind": "metric", "metric": f"metric.{NS}.customer_mrr"},
                           "right": {"kind": "literal", "value": 12}},
        },
        "customer_balance": {
            "label": "Customer balance", "kind": "derived", "value_type": "currency",
            "temporal_role": CLOCK,
            "expression": {"kind": "scoped_aggregate", "measure": MRR_ALL,
                           "aggregation": "last_value", "where": [customers]},
        },
    }})  # fmt: skip
    with duckdb.connect(str(root / "billing.duckdb")) as connection:
        connection.execute(SEED)
    return root


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    runtime = Runtime.from_path(str(_package(tmp_path_factory.mktemp("billing") / NS)))
    try:
        runtime._get_adapter()  # the module fixture owns its connection across tests
        yield runtime
    finally:
        runtime.close()


def _gold(day: str, *, filled: bool = True) -> float | None:
    """The customers' balance on ``day``: one row per account a day, so its last value."""

    total = (
        "SELECT SUM(mrr) FROM account_day JOIN accounts USING (account_id) "
        f"WHERE segment = 'customer' AND day = DATE '{day}'"
    )
    sql = f"SELECT COALESCE(({total}), 0)" if filled else total
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        return connection.execute(sql).fetchone()[0]


def _plan(engine: Runtime, intent: str, **partial: Any) -> dict[str, Any]:
    return plan_payload(engine, intent=intent, partial_query={"policy_context": NOW, **partial})


def _rows(engine: Runtime, query: dict[str, Any]) -> list[tuple[str, float]]:
    alias = query["select"][0]["as"]
    rows = engine.query({**query, "policy_context": NOW})["rows"]
    return [(str(row[f"{CLOCK}__day"])[:10], row[alias]) for row in rows]


def _stock_gaps(plan: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = plan.get("why", {}).get("details", {}).get("gaps", [])
    return [gap for gap in gaps if gap["kind"] == "stock_as_of_unrealized"]


def _assert_held(plan: dict[str, Any]) -> None:
    assert plan["status"] == "low_confidence", plan.get("why")
    assert "execute" not in plan.get("next", {}).get("ready_for", [])


def test_the_reference_values() -> None:
    assert _gold("2026-10-04") == 599
    assert _gold("2026-09-30") == 198
    # Neither customer has a row on 2026-09-15: the balance alone is NULL, the metric 0.
    assert _gold("2026-09-15", filled=False) is None
    assert _gold("2026-09-15") == 0


TODAY = "2026-10-05 isn't complete yet, so this draft can't read {}."
UNPROVEN = "This draft's window isn't proven to end on a complete day, so it can't read {}."


HELD = {
    "{} today": TODAY,
    "{} on 2026-10-05": TODAY,
    # No window words: every day up to now, today's included.
    "daily {}": UNPROVEN,
    # Open to today.
    "daily {} this month": TODAY,
}


@pytest.mark.parametrize("label", list(METRICS))
@pytest.mark.parametrize("intent", list(HELD))
def test_a_wrapped_balance_is_held_on_a_day_not_yet_complete(
    engine: Runtime, label: str, intent: str
) -> None:
    """``ok`` with ``execute`` before: the wrapped metric skipped the complete-day hold."""

    plan = _plan(engine, intent.format(label))
    _assert_held(plan)
    query = plan["best"]["query_ir"]
    assert query["select"][0]["expression"] == {"metric": METRICS[label][0]}
    assert query["time"]["grain"] == "day"
    [gap] = _stock_gaps(plan)
    assert gap["expected"] == {"grain": "day", "stocks": [MRR_ALL]}
    assert gap["clause"] == label
    assert gap["message"] == HELD[intent].format(label)
    assert "Read 2026-10-04, the last complete day, or an earlier day." in [
        hint["message"] for hint in plan["why"]["recovery_hints"]
    ]


@pytest.mark.parametrize("label", list(METRICS))
@pytest.mark.parametrize(
    "time",
    [
        {"temporal_role": CLOCK, "grain": "day", "start": "2026-10-05", "end": "2026-10-06"},
        {"temporal_role": CLOCK, "grain": "day"},
    ],
    ids=["today", "open"],
)
def test_a_caller_draft_reading_a_day_not_yet_complete_is_held(
    engine: Runtime, label: str, time: dict[str, Any]
) -> None:
    """The caller states the select and the window, so no planner drafting is involved."""

    metric = METRICS[label][0]
    select = [{"as": "value", "expression": {"metric": metric}}]
    plan = _plan(engine, label, select=select, time=time)
    _assert_held(plan)
    [gap] = _stock_gaps(plan)
    assert gap["expected"]["stocks"] == [MRR_ALL]


@pytest.mark.parametrize("label", list(METRICS))
@pytest.mark.parametrize("intent", ["{}", "{} this month", "{} as of yesterday"])
def test_these_were_held_already(engine: Runtime, label: str, intent: str) -> None:
    """Unchanged: no one-day read (no time block, a month bucket), or an as-of phrase plan
    doesn't resolve for a wrapped balance."""

    _assert_held(_plan(engine, intent.format(label)))


@pytest.mark.parametrize("label", list(METRICS))
@pytest.mark.parametrize(
    ("intent", "day"),
    [
        ("{} yesterday", "2026-10-04"),
        ("{} on 2026-09-30", "2026-09-30"),
        # A day without customer rows answers 0, where the balance alone is NULL.
        ("{} on 2026-09-15", "2026-09-15"),
    ],
)
def test_a_complete_day_is_still_answered(
    engine: Runtime, label: str, intent: str, day: str
) -> None:
    plan = _plan(engine, intent.format(label))
    assert plan["status"] == "ok", plan.get("why")
    assert "execute" in plan["next"]["ready_for"]
    query = plan["best"]["query_ir"]
    end = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    assert query["time"] == {"temporal_role": CLOCK, "grain": "day", "start": day, "end": end}
    assert _rows(engine, query) == [(day, _gold(day) * METRICS[label][1])]


@pytest.mark.parametrize("label", list(METRICS))
def test_a_closed_window_ending_on_the_last_complete_day_is_still_answered(
    engine: Runtime, label: str
) -> None:
    """Last week ends on 2026-10-04, the last complete day: each day's value is unchanged."""

    plan = _plan(engine, f"daily {label} last week")
    assert plan["status"] == "ok", plan.get("why")
    assert "execute" in plan["next"]["ready_for"]
    days = [(date(2026, 9, 28) + timedelta(days=offset)).isoformat() for offset in range(7)]
    expected = [(day, _gold(day) * METRICS[label][1]) for day in days]
    assert _rows(engine, plan["best"]["query_ir"]) == expected
