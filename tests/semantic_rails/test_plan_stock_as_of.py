"""plan holds a balance draft that reads more than one day per answer row.

A stock answers with each series' last snapshot in each period, then adds up the series. When
a series stops reporting (an account closes and its daily rows stop), a period, or a read with
no time block, keeps that series' last value, so the draft is held unless each row reads one
day. Accounts a, b and d hold 99 a day through 2026-10-04 on the basic plan; e holds 500 on
the pro plan through 2026-09-15 and f 50 through 2026-09-30. A stock keyed by its clock alone
(a daily rollup) is one series and is unchanged. Reference values come from plain SQL.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime

NOW = {"now": "2026-10-05T06:00:00Z"}
SEED = """
CREATE TABLE account_day AS
SELECT account_id, CAST(day AS DATE) AS day, plan, mrr
FROM (VALUES ('a', 'basic', 99, DATE '2026-10-04'), ('b', 'basic', 99, DATE '2026-10-04'),
             ('d', 'basic', 99, DATE '2026-10-04'), ('e', 'pro', 500, DATE '2026-09-15'),
             ('f', 'pro', 50, DATE '2026-09-30')) AS t(account_id, plan, mrr, last_day),
     generate_series(DATE '2026-09-01', last_day, INTERVAL 1 DAY) AS s(day);
CREATE TABLE user_totals AS
SELECT CAST(day AS DATE) AS day, 10 * date_diff('day', DATE '2026-09-01', day) + 10 AS registered
FROM generate_series(DATE '2026-09-01', DATE '2026-10-04', INTERVAL 1 DAY) AS s(day);
"""
MEASURE = "measure.billing.mrr_all"
STOCK = "stock_as_of_unrealized"


def _package(root: Path, *, metric: bool) -> Path:
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
    }}})  # fmt: skip
    put("models/account_day.yml", {"model": {
        "id": "account_day", "relation": "account_day", "entities": {"account_day": {}},
        "times": {"day": {"column": "day", "kind": "date", "class": "as_of_time", "default": True}},
        "dimensions": {"plan": {"kind": "categorical", "domain": ["basic", "pro"]}},
        "measures": {"mrr_all": {
            "expr": "mrr", "accumulation": stock,
            **({"publish": False} if metric else {"label": "MRR"}),
        }},
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
            "temporal_role": "temporal_role.billing_account_day_day",
            "expression": {"kind": "semi_additive", "measure": MEASURE},
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


def _plan(runtime: Runtime, intent: str, **kwargs: Any) -> dict[str, Any]:
    partial = {"policy_context": NOW, **kwargs.pop("partial", {})}
    return plan_payload(runtime, intent=intent, partial_query=partial, **kwargs)
