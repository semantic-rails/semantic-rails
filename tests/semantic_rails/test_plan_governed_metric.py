"""plan answers with the governed metric, not the measure it filters.

The neutral package counts stores from their visits. ``Active stores (all kinds)`` counts
every store with a visit; the governed metric ``Active stores`` keeps the retail ones, leaving
out demo and staff stores. A question that names the metric gets the metric's number; a draft
that still reads the measure is never ``ok``. With ``publish: false`` the measure is a
building block: discover doesn't offer it, plan answers with the metric, and a query that
names the measure by id still runs. Gold values come from plain SQL over the seed.

A second package counts teams. ``Teams (all classes)`` counts every team; the governed
``New teams`` counts customer teams only, either by filtering that count (``same``) or by
counting ``team_created`` events of customer teams (``event``). A draft that counts every
team is held unless the question asks for the class or the caller selects the count.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.http_core import SemanticHTTPService, normalize_route
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import discover_payload
from semantic_rails.planner import faithfulness, plan_payload
from semantic_rails.planner.patterns import metric_by_dimension_rollup
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

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


def _package(
    root: Path,
    *,
    publish: bool,
    label: str = "Active stores",
    domain: bool = True,
    schema_strict: bool = True,
) -> Path:
    def put(name: str, doc: dict[str, Any]) -> None:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    put("package.yml", {
        "schema_version": 1,
        "package": {"id": "shop", "namespace": "shop", "name": "shop", "description": "Stores",
                    "warehouse": "duckdb", "default_db": "shop.duckdb", "seed": {"kind": "external"},
                    "schema_strict": schema_strict},
        "defaults": {"time": {"timezone": "UTC"}},
    })  # fmt: skip
    put("graph.yml", {"graph": {"entities": {
        "visit": {"key": ["visit_id"], "model": "visits"},
    }}})  # fmt: skip
    put("models/visits.yml", {"model": {
        "id": "visits", "relation": "visits", "entities": {"visit": {}},
        "times": {"day": {"column": "day", "kind": "date", "class": "event_time", "default": True}},
        "dimensions": {"channel": {"kind": "categorical",
                                   **({"domain": ["retail", "demo", "staff"]} if domain else {})}},
        "measures": {"active_stores_all_kinds": {
            "label": "Active stores (all kinds)", "kind": "entity_count", "entity_key": "store_id",
            "value_type": "count", **({} if publish else {"publish": False}),
        }},
    }})  # fmt: skip
    put("metrics/stores.yml", {"metrics": {"active_stores": {
        "label": label, "description": "Retail stores with a visit.",
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


def test_a_word_only_the_metric_names_is_read_by_the_metric(tmp_path: Path) -> None:
    """Not ready before: "retail" named nothing the measure's draft used. The metric's own
    name now consumes it, and its number is the retail stores' one."""

    root = _package(tmp_path / "shop", publish=True, label="Active retail stores", domain=False)
    engine = Runtime.from_path(str(root))
    try:
        plan = _plan(engine, "How many retail stores were active last week?")
        assert plan["status"] == "ok", plan.get("why")
        query = plan["best"]["query_ir"]
        assert query["select"][0]["expression"] == {"metric": METRIC}
        assert _value(engine, query) == _gold(f"{LAST_WEEK} AND channel = 'retail'") == 3
    finally:
        engine.close()


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
        "version": 1,
        "select": [{"as": "all_kinds", "expression": {"measure": MEASURE}}],
        "time": {"temporal_role": "temporal_role.shop_visit_day", "range": {"last": {"unit": "week", "value": 1}}},
    }  # fmt: skip
    assert _value(runtime, query) == _gold(LAST_WEEK) == 5


def test_plan_answers_a_building_block_with_its_only_metric(runtime: Runtime) -> None:
    """The question names neither. The building block is answered with the metric; a draft
    over the published measure is held, since the metric leaves out some of its stores."""

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
    else:
        assert plan["status"] == "low_confidence"
        assert "execute" not in plan["next"].get("ready_for", [])
        assert [gap["expected"] for gap in _gaps(plan)] == [
            {"metrics": [METRIC], "narrowed_by": ["dimension.shop_visit_channel"]}
        ]


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


@pytest.mark.parametrize(
    ("schema_strict", "plain_draft"),
    [(True, False), (False, False), (False, True)],
    ids=["strict", "non_strict", "plain_metric"],
)
@pytest.mark.parametrize(
    ("intent", "status", "value"),
    [
        ("How many stores that were active last week?", "low_confidence", 5),
        ("Number of stores that were active last week", "low_confidence", 5),
        (QUESTION, "ok", 3),
    ],
)
def test_the_whole_question_is_checked_against_reference_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_strict: bool,
    plain_draft: bool,
    intent: str,
    status: str,
    value: int,
) -> None:
    root = _package(tmp_path / "shop", publish=True, schema_strict=schema_strict)
    engine = Runtime.from_path(str(root))
    try:
        if plain_draft:
            # Exercise a draft selecting the non-strict package's auto-published plain metric.
            plain = next(metric for metric in engine._config.metric_recipes if metric.id != METRIC)
            monkeypatch.setattr(metric_by_dimension_rollup, "_preferred_measure", lambda *_: None)
            monkeypatch.setattr(metric_by_dimension_rollup, "_preferred_metric", lambda *_: plain)
        plan = _plan(engine, intent)
        assert plan["status"] == status, plan.get("why")
        query = plan["best"]["query_ir"]
        assert _value(engine, query) == value
        assert value == _gold(
            LAST_WEEK if status == "low_confidence" else f"{LAST_WEEK} AND channel = 'retail'"
        )
        assert _gold(f"{LAST_WEEK} AND channel = 'retail'") == 3
        if status == "low_confidence":
            assert "execute" not in plan["next"].get("ready_for", [])
            assert [gap["expected"]["metrics"] for gap in _gaps(plan)] == [[METRIC]]
            if plain_draft:
                assert query["select"][0]["expression"]["metric"] != METRIC
            else:
                assert query["select"][0]["expression"]["measure"] == MEASURE
    finally:
        engine.close()


@pytest.mark.parametrize("intent", ["active stores all kinds last week", "stores last week"])
@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
def test_hidden_governing_metrics_are_never_selected_or_disclosed(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, intent: str, detail: str
) -> None:
    hidden_id, hidden_label = "metric.shop.restricted_cohort", "Restricted cohort"
    hidden = replace(runtime._config.metric_recipes[0], id=hidden_id, label=hidden_label)
    policy = SemanticPolicyConfig(
        id="policy.hide_cohort",
        kind="object_visibility",
        object_ids=[hidden_id],
        action="hidden",
        audiences=["external"],
    )
    monkeypatch.setattr(
        runtime,
        "_config",
        replace(
            runtime._config,
            metric_recipes=[hidden],
            semantic_policies=[policy],
        ),
    )
    arguments = {
        "intent": intent,
        "query": {"policy_context": {**NOW, "audience": "external"}},
        "detail": detail,
    }
    mcp = SemanticLayerMCPAdapter(runtime).call_tool("plan", arguments)
    http, status = SemanticHTTPService(runtime).handle(
        "POST", normalize_route("/api/v1/plan"), arguments
    )
    assert status == 200
    building_block = not runtime._config.measures[0].publish
    for response in (mcp, http):
        serialized = json.dumps(response)
        assert hidden_id not in serialized
        assert hidden_label not in serialized
        expression = response["best"]["query_ir"]["select"][0]["expression"]
        assert expression["measure"] == MEASURE
        if building_block:
            assert response["status"] == "low_confidence", response.get("why")
            assert "execute" not in response.get("next", {}).get("ready_for", [])
            assert [gap["expected"]["metrics"] for gap in _gaps(response)] == [[]]
        else:
            assert response["status"] == "ok", response.get("why")
            assert _gaps(response) == []
    # A building block stays one when its governor is hidden: discover still leaves it out.
    discovered = SemanticLayerMCPAdapter(runtime).call_tool(
        "discover",
        {"terms": "active stores", "policy_context": arguments["query"]["policy_context"]},
    )
    assert [row["id"] for row in discovered["measures"]] == ([] if building_block else [MEASURE])
    # The same policy leaves the metric visible to a different caller, on the next call.
    internal = _plan(runtime, intent, policy_context={**NOW, "audience": "internal"})
    assert hidden_id in [subject["id"] for subject in internal["intent_ir"]["subjects"]]


@pytest.mark.parametrize("both_compatible", [False, True], ids=["own_clock", "both_clocks"])
@pytest.mark.parametrize("publish", [True, False], ids=["published", "building_block"])
def test_a_governed_swap_cannot_change_the_metrics_clock(
    tmp_path: Path, publish: bool, both_compatible: bool
) -> None:
    root = _package(tmp_path / "shop", publish=publish)
    model_path = root / "models/visits.yml"
    model = yaml.safe_load(model_path.read_text())
    model["model"]["times"]["ship_time"] = {
        "column": "ship_day",
        "kind": "date",
        "class": "event_time",
    }
    model_path.write_text(yaml.safe_dump(model))
    metrics_path = root / "metrics/stores.yml"
    metrics = yaml.safe_load(metrics_path.read_text())
    metric = metrics["metrics"]["active_stores"]
    metric["temporal_role"] = "temporal_role.shop_visit_ship_time"
    metric["compatible_temporal_roles"] = [metric["temporal_role"]]
    if both_compatible:
        metric["compatible_temporal_roles"].append("temporal_role.shop_visit_day")
    metrics_path.write_text(yaml.safe_dump(metrics))
    with duckdb.connect(str(root / "shop.duckdb")) as connection:
        connection.execute("ALTER TABLE visits ADD COLUMN ship_day DATE")
        connection.execute("UPDATE visits SET ship_day = day + INTERVAL '7 days'")
    engine = Runtime.from_path(str(root))
    try:
        plan = _plan(engine, QUESTION)
        assert plan["status"] == "low_confidence", plan.get("why")
        assert "execute" not in plan["next"].get("ready_for", [])
        query = plan["best"]["query_ir"]
        assert query["select"][0]["expression"]["measure"] == MEASURE
        assert query["time"]["temporal_role"] == "temporal_role.shop_visit_day"
        assert [gap["expected"]["metrics"] for gap in _gaps(plan)] == [[METRIC]]
        assert _value(engine, query) == _gold(LAST_WEEK) == 5
        with duckdb.connect(":memory:") as connection:
            connection.execute(SEED)
            actual = connection.execute(
                "SELECT COUNT(DISTINCT store_id) FROM visits WHERE channel = 'retail' "
                "AND day + INTERVAL '7 days' >= DATE '2026-09-28' "
                "AND day + INTERVAL '7 days' < DATE '2026-10-05'"
            ).fetchone()
        assert actual == (1,)
    finally:
        engine.close()


TEAMS_SEED = """
CREATE TABLE teams (team_id VARCHAR, created_at TIMESTAMP, class VARCHAR);
INSERT INTO teams VALUES
  ('t1', TIMESTAMP '2026-09-28 10:00', 'customer'),
  ('t2', TIMESTAMP '2026-09-30 10:00', 'customer'),
  ('t3', TIMESTAMP '2026-10-02 10:00', 'customer'),
  ('t4', TIMESTAMP '2026-10-03 10:00', 'test'),
  ('t5', TIMESTAMP '2026-09-21 10:00', 'customer'),
  ('t6', TIMESTAMP '2026-09-15 10:00', 'test'),
  ('t7', TIMESTAMP '2026-09-16 10:00', 'test');
CREATE TABLE team_events (event_id VARCHAR, team_id VARCHAR, event_type VARCHAR, event_time TIMESTAMP);
INSERT INTO team_events SELECT 'c-' || team_id, team_id, 'team_created', created_at FROM teams;
INSERT INTO team_events VALUES
  ('j1', 't1', 'member_joined', TIMESTAMP '2026-09-29 10:00'),
  ('j2', 't4', 'member_joined', TIMESTAMP '2026-10-03 11:00');
"""
WEEK = "created_at >= TIMESTAMP '2026-09-28' AND created_at < TIMESTAMP '2026-10-05'"
MONTH = "created_at >= TIMESTAMP '2026-09-01' AND created_at < TIMESTAMP '2026-10-01'"
TEAMS = "measure.org.teams"
NEW_TEAMS = "metric.org.new_teams"
TEAM_CLASS = "dimension.org_team_class"
SHAPES = ["same", "event"]
HIDDEN_GOVERNOR = (
    "A definition you can't see governs this measure, so it can't be answered as a raw number."
)


def _teams_package(
    root: Path,
    *,
    shape: str,
    synonyms: bool,
    publish: bool = True,
    schema_strict: bool = True,
    filter_class: bool = True,
) -> Path:
    def put(name: str, doc: dict[str, Any]) -> None:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    customer = {"field": TEAM_CLASS, "op": "=", "value": "customer"}
    put("package.yml", {
        "schema_version": 1,
        "package": {"id": "org", "namespace": "org", "name": "org", "description": "Teams",
                    "warehouse": "duckdb", "default_db": "org.duckdb", "seed": {"kind": "external"},
                    "schema_strict": schema_strict},
        "defaults": {"time": {"timezone": "UTC"}},
    })  # fmt: skip
    put("graph.yml", {"graph": {"entities": {
        "team": {"key": ["team_id"], "model": "teams", "allowed_as_root": True},
        "team_event": {"key": ["event_id"], "model": "team_events", "allowed_as_root": True},
    }}})  # fmt: skip
    put("models/teams.yml", {"model": {
        "id": "teams", "label": "Teams", "relation": "teams", "entities": {"team": {}},
        "times": {"created_at": {"label": "Team created", "column": "created_at",
                                 "kind": "timestamp", "class": "event_time", "default": True}},
        "dimensions": {"class": {"kind": "categorical", "label": "Team class",
                                 "domain": ["customer", "test"]}},
        "measures": {"teams": {"kind": "entity_count", "entity_key": "team_id",
                               "label": "Teams (all classes)", "value_type": "count",
                               "publish": publish}},
    }})  # fmt: skip
    put("models/team_events.yml", {"model": {
        "id": "team_events", "label": "Team events", "relation": "team_events",
        "entities": {"team_event": {}, "team": {}},
        "times": {"event_time": {"label": "Event time", "column": "event_time",
                                 "kind": "timestamp", "class": "event_time", "default": True}},
        "dimensions": {"event_type": {"kind": "categorical", "label": "Event type"}},
        "measures": {"team_events": {"kind": "entity_count", "entity_key": "event_id",
                                     "label": "Team events (all classes)", "value_type": "count"}},
    }})  # fmt: skip
    if shape == "same":
        metric = {
            "kind": "aggregate", "temporal_role": "temporal_role.org_team_created_at",
            "expression": {"kind": "aggregate", "measure": TEAMS, "aggregation": "count_distinct",
                           "filter": {"all": [customer]}},
        }  # fmt: skip
    else:
        created = {
            "field": "dimension.org_team_event_event_type",
            "op": "=",
            "value": "team_created",
        }
        metric = {
            "kind": "aggregate", "temporal_role": "temporal_role.org_team_event_event_time",
            "expression": {"kind": "aggregate", "measure": "measure.org.team_events",
                           "aggregation": "count_distinct",
                           "filter": {"all": [created, *([customer] if filter_class else [])]}},
        }  # fmt: skip
    put("metrics/teams.yml", {"metrics": {"new_teams": {
        "label": "New teams",
        "description": "Customer teams created in the period." if filter_class else "Teams created in the period.",
        "value_type": "count", **metric,
        **({"synonyms": ["teams created", "created teams"]} if synonyms else {}),
    }}})  # fmt: skip
    with duckdb.connect(str(root / "org.duckdb")) as connection:
        connection.execute(TEAMS_SEED)
    return root


@pytest.fixture(scope="module")
def teams(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[tuple[str, bool], Runtime]]:
    """One runtime per metric shape, with and without a "teams created" synonym."""

    engines: dict[tuple[str, bool], Runtime] = {}
    try:
        for shape in SHAPES:
            for synonyms in (False, True):
                root = tmp_path_factory.mktemp(f"teams_{shape}") / "org"
                engine = Runtime.from_path(
                    str(_teams_package(root, shape=shape, synonyms=synonyms))
                )
                engines[shape, synonyms] = engine
                engine._get_adapter()
        yield engines
    finally:
        for engine in engines.values():
            engine.close()


def _teams_gold(where: str) -> int:
    with duckdb.connect(":memory:") as connection:
        connection.execute(TEAMS_SEED)
        return int(connection.execute(f"SELECT COUNT(*) FROM teams WHERE {where}").fetchone()[0])


@pytest.fixture(scope="module")
def unpublished_teams(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[bool, Runtime]]:
    engines = {}
    try:
        for strict in (True, False):
            root = _teams_package(
                tmp_path_factory.mktemp("unpublished_teams") / "org",
                shape="event",
                synonyms=False,
                publish=False,
                schema_strict=strict,
                filter_class=False,
            )
            engine = engines[strict] = Runtime.from_path(str(root))
            engine._get_adapter()
        yield engines
    finally:
        for engine in engines.values():
            engine.close()


@pytest.mark.parametrize("intent", ["How many teams were created last week?", "teams last week"])
@pytest.mark.parametrize("detail", ["best", "full"])
def test_strict_unpublished_measure_without_a_governor_is_held(
    unpublished_teams: dict[bool, Runtime], intent: str, detail: str
) -> None:
    plan = plan_payload(
        unpublished_teams[True], intent=intent, partial_query={"policy_context": NOW}, detail=detail
    )
    _assert_unoffered_hold(plan)


def _assert_unoffered_hold(plan: dict[str, Any]) -> None:
    assert plan["status"] == "low_confidence", plan.get("why")
    assert "execute" not in plan.get("next", {}).get("ready_for", [])
    gaps = _gaps(plan)
    assert [(gap["expected"], gap["actual"]) for gap in gaps] == [
        ({"metrics": []}, {"measure": TEAMS})
    ]
    assert gaps[0]["message"] == "The package doesn't offer this measure."
    assert [hint["message"] for hint in plan["why"]["recovery_hints"]] == [
        "Name the measure by id in partial_query.select when the question asks for every row "
        "it counts, or pick an offered metric."
    ]


@pytest.mark.parametrize(
    "metadata",
    [
        {"policy_context": {**NOW, "measure": TEAMS}},
        {"request_context": {"field": TEAMS}},
        {"request_id": {"metric": TEAMS}},
        {"_annotation": {"nested": [{"measure": TEAMS}]}},
    ],
    ids=["policy_context", "request_context", "request_id", "unknown"],
)
def test_request_metadata_cannot_name_an_unpublished_measure(
    unpublished_teams: dict[bool, Runtime], metadata: dict[str, Any]
) -> None:
    plan = plan_payload(
        unpublished_teams[True],
        intent="teams last week",
        partial_query={"policy_context": NOW, **metadata},
        detail="best",
    )
    _assert_unoffered_hold(plan)


def _transport_plan(engine: Runtime, transport: str, query: dict[str, Any]) -> dict[str, Any]:
    arguments = {"intent": "teams last week", "query": query, "detail": "best"}
    if transport == "direct":
        return plan_payload(engine, intent="teams last week", partial_query=query, detail="best")
    if transport == "mcp":
        return SemanticLayerMCPAdapter(engine).call_tool("plan", arguments)
    plan, status = SemanticHTTPService(engine).handle(
        "POST", normalize_route("/api/v1/plan"), arguments
    )
    assert status == 200
    return plan


def _unreadable_offer(*_: Any) -> Any:
    raise RuntimeError("unreadable measure offering")


@pytest.mark.parametrize("transport", ["mcp", "http"])
def test_transport_plan_holds_a_measure_named_only_in_policy_metadata(
    unpublished_teams: dict[bool, Runtime], transport: str
) -> None:
    query = {"policy_context": {**NOW, "measure": TEAMS}}
    _assert_unoffered_hold(_transport_plan(unpublished_teams[True], transport, query))


@pytest.mark.parametrize("offer", ["readable", "unreadable"])
@pytest.mark.parametrize("transport", ["direct", "mcp", "http"])
@pytest.mark.parametrize("option", ["debug", "explain", "export"])
def test_a_response_option_cannot_name_an_unpublished_measure(
    unpublished_teams: dict[bool, Runtime],
    monkeypatch: pytest.MonkeyPatch,
    option: str,
    transport: str,
    offer: str,
) -> None:
    """Only ``partial_query.select`` makes a choice; an option's nested value never does."""

    if offer == "unreadable":
        monkeypatch.setattr(faithfulness, "unoffered_measures", _unreadable_offer)
    query = {"policy_context": NOW, option: {"measure": TEAMS}}
    _assert_unoffered_hold(_transport_plan(unpublished_teams[True], transport, query))


@pytest.mark.parametrize(
    "request_fields",
    [
        {"policy_context": {**NOW, "measure": MEASURE}},
        {"policy_context": NOW, "debug": {"measure": MEASURE}},
    ],
    ids=["policy_context", "debug"],
)
def test_request_fields_outside_select_do_not_exempt_a_draft_over_a_governed_measure(
    runtime: Runtime, request_fields: dict[str, Any]
) -> None:
    plan = plan_payload(
        runtime,
        intent="active stores all kinds last week",
        partial_query=request_fields,
        detail="best",
    )
    assert plan["status"] == "low_confidence", plan.get("why")
    assert "execute" not in plan.get("next", {}).get("ready_for", [])
    assert [(gap["expected"], gap["actual"]) for gap in _gaps(plan)] == [
        ({"metrics": [METRIC]}, {"measure": MEASURE})
    ]
    assert [hint["message"] for hint in plan["why"]["recovery_hints"]] == [
        "Select the governed metric in Query IR. Name the measure by id in "
        "partial_query.select only when the question asks for every row it counts."
    ]


@pytest.mark.parametrize(
    ("outside", "code"),
    [
        ({"order_by": [{"field": TEAMS, "direction": "desc"}]}, "VALIDATION_FAILED"),
        (
            {"where": [{"field": TEAM_CLASS, "op": "=", "value": {"measure": TEAMS}}]},
            "VALIDATION_FAILED",
        ),
        (
            {"metric_filters": [{"expression": {"measure": TEAMS}, "op": ">", "value": 0}]},
            "PLAN_INTENT_COVERAGE_GAP",
        ),
    ],
    ids=["order_by", "where_value", "metric_filters"],
)
def test_a_measure_named_only_outside_select_is_never_executable(
    unpublished_teams: dict[bool, Runtime], outside: dict[str, Any], code: str
) -> None:
    plan = _plan(unpublished_teams[True], "teams last week", **outside)
    assert plan["status"] == "low_confidence", plan.get("why")
    assert "execute" not in plan.get("next", {}).get("ready_for", [])
    assert plan["why"]["code"] == code
    if code == "PLAN_INTENT_COVERAGE_GAP":
        _assert_unoffered_hold(plan)


def test_mcp_plan_holds_a_strict_unpublished_measure(
    unpublished_teams: dict[bool, Runtime],
) -> None:
    plan = SemanticLayerMCPAdapter(unpublished_teams[True]).call_tool(
        "plan",
        {"intent": "How many teams were created last week?", "query": {"policy_context": NOW}},
    )
    _assert_unoffered_hold(plan)


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "non_strict"])
def test_only_strict_unpublished_measures_are_removed_from_discover(
    unpublished_teams: dict[bool, Runtime], strict: bool
) -> None:
    found = discover_payload(unpublished_teams[strict], terms="teams, created")
    assert (TEAMS in [row["id"] for row in found["measures"]]) is not strict
    assert NEW_TEAMS in [row["id"] for row in found["metrics"]]


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit_measure", "named_metric"])
def test_explicit_unpublished_measure_and_named_event_metric_keep_their_reference_numbers(
    unpublished_teams: dict[bool, Runtime], explicit: bool
) -> None:
    partial = {"select": [{"as": "teams", "expression": {"measure": TEAMS}}]} if explicit else {}
    plan = _plan(
        unpublished_teams[True],
        "teams last week" if explicit else "new teams last week",
        **partial,
    )
    assert plan["status"] == "ok", plan.get("why")
    assert "execute" in plan["next"]["ready_for"]
    query = plan["best"]["query_ir"]
    expression = query["select"][0]["expression"]
    assert expression.get("measure" if explicit else "metric") == (TEAMS if explicit else NEW_TEAMS)
    with duckdb.connect(":memory:") as connection:
        connection.execute(TEAMS_SEED)
        sql = (
            f"SELECT COUNT(DISTINCT team_id) FROM teams WHERE {WEEK}"
            if explicit
            else "SELECT COUNT(DISTINCT event_id) FROM team_events WHERE event_type = 'team_created' "
            "AND event_time >= TIMESTAMP '2026-09-28' AND event_time < TIMESTAMP '2026-10-05'"
        )
        reference = connection.execute(sql).fetchone()[0]
    assert _value(unpublished_teams[True], query) == reference == 4


@pytest.mark.parametrize("intent", ["How many teams were created last week?", "teams last week"])
def test_non_strict_publish_false_only_suppresses_auto_publishing_and_keeps_plans_ok(
    unpublished_teams: dict[bool, Runtime], intent: str
) -> None:
    engine = unpublished_teams[False]
    plan = _plan(engine, intent)
    assert plan["status"] == "ok", plan.get("why")
    assert "execute" in plan["next"]["ready_for"]
    query = plan["best"]["query_ir"]
    assert query["select"][0]["expression"]["measure"] == TEAMS
    assert _value(engine, query) == _teams_gold(WEEK) == 4


@pytest.mark.parametrize("metadata_reference", [False, True], ids=["no_reference", "metadata"])
def test_a_failing_unoffered_measure_check_holds_the_draft(
    unpublished_teams: dict[bool, Runtime],
    monkeypatch: pytest.MonkeyPatch,
    metadata_reference: bool,
) -> None:
    monkeypatch.setattr(faithfulness, "unoffered_measures", _unreadable_offer)
    _assert_unoffered_hold(
        _plan(
            unpublished_teams[True],
            "teams last week",
            policy_context={**NOW, **({"measure": TEAMS} if metadata_reference else {})},
        )
    )


def test_the_team_count_counts_the_test_teams_new_teams_leaves_out() -> None:
    assert (_teams_gold(WEEK), _teams_gold(f"{WEEK} AND class = 'customer'")) == (4, 3)
    assert (_teams_gold(MONTH), _teams_gold(f"{MONTH} AND class = 'customer'")) == (5, 3)


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize(
    ("intent", "synonyms"),
    [
        ("How many teams were created last week?", False),
        ("How many teams were created last week?", True),
        ("How many teams were created last month?", False),
        ("How many teams were created last month?", True),
        ("teams created last week", False),
        ("teams last week", False),
        ("teams last week", True),
    ],
)
def test_a_draft_counting_teams_new_teams_leaves_out_is_held(
    teams: dict[tuple[str, bool], Runtime], shape: str, intent: str, synonyms: bool
) -> None:
    engine = teams[shape, synonyms]
    window = MONTH if "month" in intent else WEEK
    for detail in ("best", "full", "query", "debug"):
        plan = plan_payload(
            engine, intent=intent, partial_query={"policy_context": NOW}, detail=detail
        )
        assert plan["status"] == "low_confidence", (detail, plan.get("why"))
        assert "execute" not in plan.get("next", {}).get("ready_for", [])
        assert [(gap["expected"], gap["actual"]) for gap in _gaps(plan)] == [
            ({"metrics": [NEW_TEAMS], "narrowed_by": [TEAM_CLASS]}, {"measure": TEAMS})
        ], detail
    # The held draft counts the test teams as well.
    assert _value(engine, plan["best"]["query_ir"]) == _teams_gold(window)
    assert _teams_gold(window) > _teams_gold(f"{window} AND class = 'customer'")


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize(
    ("intent", "synonyms", "expression"),
    [
        ("new teams last week", False, {"metric": NEW_TEAMS}),
        ("teams created last week", True, {"metric": NEW_TEAMS}),
        # The draft filters by the class the metric narrows on.
        ("How many customer teams were created last week?", False, {"measure": TEAMS}),
    ],
)
def test_a_draft_counting_customer_teams_stays_ready(
    teams: dict[tuple[str, bool], Runtime],
    shape: str,
    intent: str,
    synonyms: bool,
    expression: dict[str, str],
) -> None:
    engine = teams[shape, synonyms]
    plan = _plan(engine, intent)
    assert plan["status"] == "ok", plan.get("why")
    assert "execute" in plan["next"]["ready_for"]
    query = plan["best"]["query_ir"]
    selected = query["select"][0]["expression"]
    assert {key: value for key, value in selected.items() if key != "aggregation"} == expression
    assert _value(engine, query) == _teams_gold(f"{WEEK} AND class = 'customer'") == 3


@pytest.mark.parametrize("shape", SHAPES)
def test_a_caller_selecting_the_team_count_counts_every_team(
    teams: dict[tuple[str, bool], Runtime], shape: str
) -> None:
    engine = teams[shape, False]
    select = [{"as": "teams", "expression": {"measure": TEAMS}}]
    plan = _plan(engine, "teams created last week", select=select)
    assert plan["status"] == "ok", plan.get("why")
    assert _gaps(plan) == []
    assert _value(engine, plan["best"]["query_ir"]) == _teams_gold(WEEK) == 4


def test_mcp_plan_holds_the_count_of_every_team(teams: dict[tuple[str, bool], Runtime]) -> None:
    mcp = SemanticLayerMCPAdapter(teams["event", False])
    plan = mcp.call_tool(
        "plan",
        {"intent": "How many teams were created last week?", "query": {"policy_context": NOW}},
    )
    assert plan["status"] == "low_confidence", plan.get("why")
    assert "execute" not in plan.get("next", {}).get("ready_for", [])
    assert [gap["expected"]["metrics"] for gap in _gaps(plan)] == [[NEW_TEAMS]]


@pytest.mark.parametrize("shape", SHAPES)
def test_the_hold_names_the_question_s_metrics_first_and_at_most_five(
    teams: dict[tuple[str, bool], Runtime], shape: str
) -> None:
    config = teams[shape, False]._config
    governor = next(row for row in config.metric_recipes if row.id == NEW_TEAMS)
    ids = [f"metric.org.governor_{index}" for index in range(7)]
    config = replace(config, metric_recipes=[replace(governor, id=row) for row in ids])
    hold = faithfulness._population_hold
    # ids[0] is selected; the question's subjects rank ids[6] above ids[3].
    assert hold(config, TEAMS, {}, [ids[0]], [ids[6], TEAMS, ids[3]]) == {
        "metrics": [ids[6], ids[3], ids[1], ids[2], ids[4]],
        "narrowed_by": [TEAM_CLASS],
    }
    test_teams = {"where": [{"field": TEAM_CLASS, "op": "=", "value": "test"}]}
    for heeded in ({"group_by": [TEAM_CLASS]}, test_teams):
        assert hold(config, TEAMS, heeded, [], []) is None
    # Only the measure's own entity counts: the team class never narrows the event count, and
    # the event type narrows it only where the metric counts events.
    events = hold(config, "measure.org.team_events", {}, [], []) or {}
    event_type = ["dimension.org_team_event_event_type"] if shape == "event" else None
    assert events.get("narrowed_by") == event_type


def test_a_failing_governor_check_holds_the_draft(
    teams: dict[tuple[str, bool], Runtime], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(*_: Any) -> Any:
        raise RuntimeError("unreadable metric")

    monkeypatch.setattr(faithfulness, "population_governors", unreadable)
    plan = _plan(teams["same", False], "How many customer teams were created last week?")
    assert plan["status"] == "low_confidence", plan.get("why")
    assert "execute" not in plan["next"].get("ready_for", [])
    assert [gap["expected"] for gap in _gaps(plan)] == [{"metrics": []}]


@pytest.mark.parametrize("shape", SHAPES)
def test_a_hidden_governor_is_neither_counted_nor_named(
    teams: dict[tuple[str, bool], Runtime], monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    engine = teams[shape, False]
    policy = SemanticPolicyConfig(
        id="policy.hide_new_teams",
        kind="object_visibility",
        object_ids=[NEW_TEAMS],
        action="hidden",
        audiences=["external"],
    )
    monkeypatch.setattr(engine, "_config", replace(engine._config, semantic_policies=[policy]))
    external = {**NOW, "audience": "external"}
    for detail in ("best", "full", "query", "debug"):
        plan = plan_payload(
            engine,
            intent="How many teams were created last week?",
            partial_query={"policy_context": external},
            detail=detail,
        )
        assert plan["status"] == "ok", (detail, plan.get("why"))
        assert _gaps(plan) == []
        serialized = json.dumps(plan)
        assert NEW_TEAMS not in serialized and "New teams" not in serialized
    assert _value(engine, plan["best"]["query_ir"]) == _teams_gold(WEEK) == 4


def test_a_hidden_narrowing_dimension_still_holds_and_is_not_named(
    teams: dict[tuple[str, bool], Runtime], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = teams["same", False]
    policy = SemanticPolicyConfig(
        id="policy.hide_team_class",
        kind="object_visibility",
        object_ids=[TEAM_CLASS],
        action="hidden",
        audiences=["external"],
    )
    monkeypatch.setattr(engine, "_config", replace(engine._config, semantic_policies=[policy]))
    plan = _plan(engine, "teams last week", policy_context={**NOW, "audience": "external"})
    assert plan["status"] == "low_confidence", plan.get("why")
    assert "execute" not in plan.get("next", {}).get("ready_for", [])
    # "New teams" reads the hidden class, so it is hidden too: the hold still stands, naming
    # neither of them.
    gaps = _gaps(plan)
    assert [gap["expected"] for gap in gaps] == [{"metrics": [], "narrowed_by": []}]
    assert [gap["message"] for gap in gaps] == [HIDDEN_GOVERNOR]
    serialized = json.dumps(plan)
    for name in (TEAM_CLASS, "Team class", NEW_TEAMS, "New teams", "Customer teams created"):
        assert name not in serialized
    # The control: for a caller who sees the class, the hold names the metric and the class.
    visible = _plan(engine, "teams last week", policy_context={**NOW, "audience": "internal"})
    assert visible["status"] == "low_confidence", visible.get("why")
    assert [gap["expected"] for gap in _gaps(visible)] == [
        {"metrics": [NEW_TEAMS], "narrowed_by": [TEAM_CLASS]}
    ]
    assert [gap["message"] for gap in _gaps(visible)] != [HIDDEN_GOVERNOR]
