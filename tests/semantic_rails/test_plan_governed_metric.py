"""plan answers with the governed metric, not the measure it filters.

The neutral package counts stores from their visits. ``Active stores (all kinds)`` counts
every store with a visit; the governed metric ``Active stores`` keeps the retail ones, leaving
out demo and staff stores. A question that names the metric gets the metric's number; a draft
that still reads the measure is never ``ok``. With ``publish: false`` the measure is a
building block: discover doesn't offer it, plan answers with the metric, and a query that
names the measure by id still runs. Gold values come from plain SQL over the seed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.http_core import SemanticHTTPService, normalize_route
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import discover_payload
from semantic_rails.planner import plan_payload
from semantic_rails.planner.patterns import metric_by_dimension_rollup
from semantic_rails.runtime import Runtime

SEED = """
CREATE TABLE visits (visit_id INTEGER, store_id VARCHAR, day DATE, channel VARCHAR);
INSERT INTO visits VALUES
  (1, 's1', DATE '2026-09-28', 'retail'),
  (2, 's2', DATE '2026-09-29', 'retail'),
  (3, 's3', DATE '2026-09-30', 'retail'),
  (4, 's4', DATE '2026-10-01', 'demo'),
  (5, 's5', DATE '2026-10-02', 'staff'),
  (6, 's1', DATE '2026-10-03', 'retail'),
  (7, 's6', DATE '2026-09-21', 'retail');
"""
NOW = {"now": "2026-10-05T06:00:00Z"}  # "last week" is 2026-09-28 .. 2026-10-04
LAST_WEEK = "day >= DATE '2026-09-28' AND day < DATE '2026-10-05'"
MEASURE = "measure.shop.active_stores_all_kinds"
METRIC = "metric.shop.active_stores"
QUESTION = "How many stores were active last week?"


def _package(root: Path, *, publish: bool) -> Path:
    def put(name: str, doc: dict[str, Any]) -> None:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    put("package.yml", {
        "schema_version": 1,
        "package": {"id": "shop", "namespace": "shop", "name": "shop", "description": "Stores",
                    "warehouse": "duckdb", "default_db": "shop.duckdb", "seed": {"kind": "external"},
                    "schema_strict": True},
        "defaults": {"time": {"timezone": "UTC"}},
    })  # fmt: skip
    put("graph.yml", {"graph": {"entities": {
        "visit": {"key": ["visit_id"], "model": "visits"},
    }}})  # fmt: skip
    put("models/visits.yml", {"model": {
        "id": "visits", "relation": "visits", "entities": {"visit": {}},
        "times": {"day": {"column": "day", "kind": "date", "class": "event_time", "default": True}},
        "dimensions": {"channel": {"kind": "categorical", "domain": ["retail", "demo", "staff"]}},
        "measures": {"active_stores_all_kinds": {
            "label": "Active stores (all kinds)", "kind": "entity_count", "entity_key": "store_id",
            "value_type": "count", **({} if publish else {"publish": False}),
        }},
    }})  # fmt: skip
    put("metrics/stores.yml", {"metrics": {"active_stores": {
        "label": "Active stores", "description": "Retail stores with a visit.",
        "kind": "aggregate", "value_type": "count", "temporal_role": "temporal_role.shop_visit_day",
        "expression": {
            "kind": "aggregate", "measure": MEASURE, "aggregation": "count_distinct",
            "filter": {"all": [
                {"field": "dimension.shop_visit_channel", "op": "=", "value": "retail"},
            ]},
        },
    }}})  # fmt: skip
    with duckdb.connect(str(root / "shop.duckdb")) as connection:
        connection.execute(SEED)
    return root


@pytest.fixture(scope="module", params=[True, False], ids=["published", "building_block"])
def runtime(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    root = _package(tmp_path_factory.mktemp("governed") / "shop", publish=request.param)
    engine = Runtime.from_path(str(root))
    try:
        engine._get_adapter()  # the module fixture owns its connection across tests
        yield engine
    finally:
        engine.close()


def _gold(where: str) -> int:
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        row = connection.execute(f"SELECT COUNT(DISTINCT store_id) FROM visits WHERE {where}")
        return int(row.fetchone()[0])


def _plan(runtime: Runtime, intent: str, **partial: Any) -> dict[str, Any]:
    return plan_payload(runtime, intent=intent, partial_query={"policy_context": NOW, **partial})


def _value(runtime: Runtime, query: dict[str, Any]) -> int:
    rows = runtime.query({**query, "policy_context": NOW})["rows"]
    assert len(rows) == 1, rows
    return int(rows[0][query["select"][0]["as"]])


def _gaps(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        gap
        for gap in plan.get("why", {}).get("details", {}).get("gaps", [])
        if gap["kind"] == "governed_metric_unrealized"
    ]


def test_the_measure_counts_every_store_the_metric_keeps_retail_ones() -> None:
    assert (_gold(LAST_WEEK), _gold(f"{LAST_WEEK} AND channel = 'retail'")) == (5, 3)


@pytest.mark.parametrize("intent", [QUESTION, "stores active last week", "active stores last week"])
def test_a_question_naming_the_metric_gets_the_metric(runtime: Runtime, intent: str) -> None:
    plan = _plan(runtime, intent)
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query_ir"]
    assert query["select"] == [{"as": "active_stores", "expression": {"metric": METRIC}}]
    assert _value(runtime, query) == _gold(f"{LAST_WEEK} AND channel = 'retail'") == 3


@pytest.mark.parametrize(
    ("intent", "forced"),
    [
        # Named in full, the measure is drafted, and the metric is still named.
        ("active stores all kinds last week", False),
        # A grouping or filter on the field the metric's filter reads keeps the measure.
        ("How many demo stores were active last week?", False),
        # Any path that drafts the measure is held: here, the pattern's preference is off.
        (QUESTION, True),
    ],
)
def test_a_draft_over_the_measure_is_held_naming_the_metric(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, intent: str, forced: bool
) -> None:
    if forced:
        monkeypatch.setattr(metric_by_dimension_rollup, "_governed_target", lambda *_: None)
    plan = _plan(runtime, intent)
    assert plan["status"] == "low_confidence"
    assert "ready_for" not in plan["next"]
    assert MEASURE in plan["best"]["subject_ids_used"] or any(
        MEASURE in str(row) for row in plan["best"]["resolved"]
    )
    gap = _gaps(plan)
    assert [row["expected"]["metrics"] for row in gap] == [[METRIC]], plan["why"]
    assert gap[0]["actual"] == {"measure": MEASURE}


def test_a_filter_on_the_metric_field_reads_the_rows_it_asks_for(runtime: Runtime) -> None:
    """The held draft keeps the question's demo stores, which the metric would drop."""

    plan = _plan(runtime, "How many demo stores were active last week?")
    query = plan["best"]["query_ir"]
    assert query["select"][0]["expression"]["measure"] == MEASURE
    assert _value(runtime, query) == _gold(f"{LAST_WEEK} AND channel = 'demo'") == 1


def test_a_caller_selecting_the_measure_by_id_is_not_held(runtime: Runtime) -> None:
    select = [{"as": "all_kinds", "expression": {"measure": MEASURE}}]
    plan = _plan(runtime, "active stores all kinds last week", select=select)
    assert _gaps(plan) == []


def test_a_building_block_is_left_out_of_discover_and_runs_by_id(runtime: Runtime) -> None:
    building_block = not next(row for row in runtime._config.measures if row.id == MEASURE).publish
    found = discover_payload(runtime, terms="active stores")
    assert METRIC in [row["id"] for row in found["metrics"]]
    assert (MEASURE in [row["id"] for row in found["measures"]]) is not building_block
    query = {
        "version": 2,
        "select": [{"as": "all_kinds", "expression": {"measure": MEASURE}}],
        "time": {"temporal_role": "temporal_role.shop_visit_day", "range": {"last": {"unit": "week", "value": 1}}},
    }  # fmt: skip
    assert _value(runtime, query) == _gold(LAST_WEEK) == 5


def test_plan_answers_a_building_block_with_its_only_metric(runtime: Runtime) -> None:
    """The question names neither; only the building block is answered with the metric."""

    plan = _plan(runtime, "stores last week")
    building_block = not next(row for row in runtime._config.measures if row.id == MEASURE).publish
    expected = {"metric": METRIC} if building_block else {"measure": MEASURE}
    assert {
        key: value
        for key, value in plan["best"]["query_ir"]["select"][0]["expression"].items()
        if key != "aggregation"
    } == expected
    if building_block:
        assert plan["status"] == "ok", plan.get("why")
        assert _value(runtime, plan["best"]["query_ir"]) == 3


def test_mcp_and_http_plan_agree(runtime: Runtime) -> None:
    arguments = {"intent": QUESTION, "query": {"policy_context": NOW}, "detail": "query"}
    mcp = SemanticLayerMCPAdapter(runtime).call_tool("plan", arguments)
    http, status = SemanticHTTPService(runtime).handle(
        "POST", normalize_route("/api/v1/plan"), arguments
    )
    direct = _plan(runtime, QUESTION)
    assert status == 200
    for response in (mcp, http):
        assert response["status"] == direct["status"] == "ok"
        assert response["best"]["query_ir"] == direct["best"]["query_ir"]
        assert response["best"]["query_ir"]["select"][0]["expression"] == {"metric": METRIC}
