"""A ``PATH_NOT_FOUND`` names the same measure on another model when that one answers the call.

A call that groups or filters a measure by a dimension its model has no route to is refused.
When another model holds the same measure (the same key or label, each without its model's own
tag, totalled the same way) and the call compiles with it in place, for this caller, under this
caller's policies, the refusal names it by id with what its model says its rows are. The agent
picks; the engine never answers from a model the call didn't name. A measure the caller can't
see or may not read is never named, and ``plan`` validating its own drafts offers none.
A measure's inspect card names its look-alikes the same way, with the entities each reaches.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.diagnostics import enrich_path_not_found, exception_issue
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import _max_result_chars as max_result_chars
from semantic_rails.metadata import LOOKALIKE_NOTICE
from semantic_rails.path_gap import lookalike_swaps
from semantic_rails.planner.call import Call
from semantic_rails.planner.plan_query import _validate_query
from semantic_rails.runtime import Runtime
from semantic_rails.visible_view import view_of
from tests.semantic_rails.hidden_absent import (
    CALLER,
    absent,
    access_policy,
    envelope,
    mcp_call,
    visibility_policy,
    with_policies,
)
from tests.semantic_rails.test_runtime_and_metadata import jaffle_snapshot as jaffle_snapshot

REVENUE = "measure.shop.revenue"
ATTRIBUTED_REVENUE = "measure.shop.attributed_revenue"
CAMPAIGN = "dimension.shop_attributed_order_campaign"
ROOMS_AREA = "measure.shop.rooms_area"
HIST_AREA = "measure.shop.rooms_hist_area"
V2_AREA = "measure.shop.rooms_v2_area"
BUILDING_NAME = "dimension.shop_building_name"
LEGACY_AREA = "dimension.shop_room_legacy_area"
ATTRIBUTED = "Only orders attributed to a marketing campaign; unattributed orders are not in it."
SEED = """
CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, status VARCHAR, ordered_at DATE,
  amount DOUBLE);
INSERT INTO orders VALUES (1, 1, 'done', '2026-08-01', 10), (2, 2, 'done', '2026-09-01', 20),
  (3, 1, 'done', '2026-09-02', 40);
CREATE TABLE attributed_orders (order_id INTEGER, customer_id INTEGER, campaign VARCHAR,
  ordered_at DATE, amount DOUBLE);
INSERT INTO attributed_orders VALUES (1, 1, 'spring', '2026-08-01', 10),
  (3, 1, 'fall', '2026-09-02', 40);
CREATE TABLE customers (customer_id INTEGER, region VARCHAR);
INSERT INTO customers VALUES (1, 'north'), (2, 'south');
CREATE TABLE rooms (room_id INTEGER, building_id INTEGER, area DOUBLE);
INSERT INTO rooms VALUES (1, 1, 30), (2, 1, 20), (3, 2, 50);
CREATE TABLE rooms_hist (version_id INTEGER, room_id INTEGER, area DOUBLE);
INSERT INTO rooms_hist VALUES (1, 1, 25), (2, 1, 30), (3, 2, 20), (4, 3, 50);
CREATE TABLE rooms_legacy (room_id INTEGER, building_id INTEGER, area VARCHAR);
INSERT INTO rooms_legacy VALUES (1, 1, '30 m2');
CREATE TABLE rooms_v2 (room_id INTEGER, building_id INTEGER, area DOUBLE);
INSERT INTO rooms_v2 VALUES (1, 1, 30);
CREATE TABLE buildings (building_id INTEGER, name VARCHAR);
INSERT INTO buildings VALUES (1, 'Hall'), (2, 'Annex');
"""


def _measure(label: str, expr: str, value_type: str = "number") -> dict[str, Any]:
    return {
        "label": label,
        "kind": "aggregate",
        "expr": expr,
        "accumulation": {"kind": "flow"},
        "value_type": value_type,
    }


def _orders(model_id: str, entity: str, description: str, dimension: str, key: str, customer: bool):
    return {
        "model": {
            "id": model_id,
            "relation": model_id,
            "description": description,
            "entities": {entity: {}, **({"customer": {}} if customer else {})},
            "times": {
                "ordered_at": {
                    "column": "ordered_at",
                    "kind": "date",
                    "class": "event_time",
                    "default": True,
                }
            },
            "dimensions": {dimension: {"kind": "categorical"}},
            "measures": {
                key: _measure("Revenue", "amount", "currency"),
                # Each table's row count, as a package mirrored from warehouse tables names it.
                f"{model_id}_count": {
                    **_measure("Row count", "", "count"),
                    "expr": {"kind": "literal", "value": 1},
                },
            },
        }
    }


def _rooms(model_id: str, entity: str, description: str, *, building: bool, measure: bool):
    return {
        "model": {
            "id": model_id,
            "relation": model_id,
            "description": description,
            "entities": {entity: {}, **({"building": {}} if building else {})},
            "dimensions": {"area": {"kind": "categorical"}} if not measure else {},
            "measures": {
                f"{model_id}_area": _measure(f"Area ({model_id.upper()})", "area"),
                # A count of the model's own key: rooms here, room versions on the history copy.
                f"{model_id}_count": {
                    "label": f"Count ({model_id.upper()})",
                    "kind": "entity_count",
                    "entity_key": "version_id" if model_id == "rooms_hist" else "room_id",
                    "accumulation": {"kind": "population"},
                    "value_type": "count",
                },
            }
            if measure
            else {},
        }
    }


def _files(v2: bool) -> dict[str, Any]:
    entities = {
        "order": {"key": ["order_id"], "model": "orders"},
        "attributed_order": {"key": ["order_id"], "model": "attributed_orders"},
        "customer": {"key": ["customer_id"], "model": "customers"},
        "room": {"key": ["room_id"], "model": "rooms"},
        "room_hist": {"key": ["version_id"], "model": "rooms_hist"},
        "room_legacy": {"key": ["room_id"], "model": "rooms_legacy"},
        "building": {"key": ["building_id"], "model": "buildings"},
    }
    relationships = {
        "order_customer": {"entities": ["order", "customer"], "cardinality": "many_to_one"},
        "room_building": {"entities": ["room", "building"], "cardinality": "many_to_one"},
        "legacy_building": {
            "entities": ["room_legacy", "building"],
            "cardinality": "many_to_one",
        },
    }
    files: dict[str, Any] = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "shop",
                "namespace": "shop",
                "name": "Shop",
                "description": "Orders and rooms",
                "warehouse": "duckdb",
                "default_db": "shop.duckdb",
                "seed": {"kind": "external"},
            },
            "defaults": {"time": {"timezone": "UTC"}},
        },
        "models/orders.yml": _orders(
            "orders", "order", "One row per completed order.", "status", "revenue", True
        ),
        "models/attributed_orders.yml": _orders(
            "attributed_orders",
            "attributed_order",
            ATTRIBUTED,
            "campaign",
            "attributed_revenue",
            False,
        ),
        "models/customers.yml": {
            "model": {
                "id": "customers",
                "relation": "customers",
                "entities": {"customer": {}},
                "dimensions": {"region": {"kind": "categorical"}},
            }
        },
        "models/rooms.yml": _rooms(
            "rooms", "room", "One row per room in use.", building=True, measure=True
        ),
        "models/rooms_hist.yml": _rooms(
            "rooms_hist",
            "room_hist",
            "One row per version of a room, past and present.",
            building=False,
            measure=True,
        ),
        "models/rooms_legacy.yml": _rooms(
            "rooms_legacy", "room_legacy", "Rooms of the old system.", building=True, measure=False
        ),
        "models/buildings.yml": {
            "model": {
                "id": "buildings",
                "relation": "buildings",
                "entities": {"building": {}},
                "dimensions": {"name": {"kind": "categorical"}},
            }
        },
    }
    if v2:
        entities["room_v2"] = {"key": ["room_id"], "model": "rooms_v2"}
        relationships["v2_building"] = {
            "entities": ["room_v2", "building"],
            "cardinality": "many_to_one",
        }
        files["models/rooms_v2.yml"] = _rooms(
            "rooms_v2", "room_v2", "Rooms of the new system.", building=True, measure=True
        )
    files["graph.yml"] = {"graph": {"entities": entities, "relationships": relationships}}
    return files


@pytest.fixture(scope="module")
def shop_roots(tmp_path_factory: pytest.TempPathFactory) -> dict[bool, Path]:
    roots = {}
    for v2 in (False, True):
        roots[v2] = root = tmp_path_factory.mktemp("lookalike_shop")
        for name, body in _files(v2).items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(body), encoding="utf-8")
        with duckdb.connect(str(root / "shop.duckdb")) as connection:
            connection.execute(SEED)
    return roots


@pytest.fixture()
def shop(shop_roots: dict[bool, Path]) -> Iterator[Callable[..., Runtime]]:
    """A runtime over the shop package (``v2`` adds a second rooms copy that reaches buildings),
    serving ``change(config)`` when given."""
    runtimes: list[Runtime] = []

    def build(v2: bool = False, change: Callable[[Any], Any] | None = None) -> Runtime:
        runtime = Runtime.from_path(str(shop_roots[v2]))
        runtimes.append(runtime)
        if change is not None:
            runtime._config = change(runtime.package_config)
        return runtime

    yield build
    for runtime in runtimes:
        runtime.close()


def _validate(runtime: Runtime, query: dict[str, Any], **caller: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {"query": query, "mode": "validate", **caller}
    return mcp_call(runtime, "execute", arguments)


def _refusal(response: dict[str, Any]) -> dict[str, Any]:
    (error,) = response["errors"]
    assert error["code"] == "PATH_NOT_FOUND", error
    return error


def _hint(error: dict[str, Any]) -> dict[str, Any]:
    return next(hint for hint in error["recovery_hints"] if hint["kind"] == "use_lookalike_measure")


def _follow(call: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """``call`` as the hint says to write it from ``row``: its measures in place of the call's,
    and its renamed columns wherever the call names them (order_by, having)."""
    measures, columns = row["measures"], row.get("columns", {})
    out = {**call, "select": [measures.get(item, item) for item in call["select"]]}
    for key in ("order_by", "having"):
        if key in call:
            out[key] = [
                {**item, "field": columns.get(item["field"], measures.get(item["field"]))}
                if item["field"] in columns or item["field"] in measures
                else item
                for item in call[key]
            ]
    return out


CAMPAIGN_REVENUE = {"select": [REVENUE], "group_by": [CAMPAIGN]}
# The rooms model's objects, which hiding its entity hides with it.
ROOM_OBJECTS = [
    "entity.shop_room",
    ROOMS_AREA,
    "measure.shop.rooms_count",
    "dimension.shop_room_id",
    "dimension.shop_room_building_id",
    "relationship.room_building",
]
AREA_BY_BUILDING = {"select": [HIST_AREA], "group_by": [BUILDING_NAME]}


# --- The refusal names who can answer -----------------------------------------------------------


def test_the_one_model_holding_the_campaign_answers_and_says_its_rows(shop):
    # Revenue by campaign: the orders table has no campaign; a table of attributed orders does.
    runtime = shop()
    error = _refusal(_validate(runtime, CAMPAIGN_REVENUE))
    row = {
        "entity": "entity.shop_attributed_order",
        "measures": {REVENUE: ATTRIBUTED_REVENUE},
        "description": ATTRIBUTED,
        "columns": {"revenue": "attributed_revenue"},
    }
    assert error["details"]["lookalike_measures"] == [row]
    assert error["message"].endswith(
        "Another model's measure of the same name answers this call as it stands "
        "(details.lookalike_measures)."
    )
    hint = _hint(error)
    assert error["recovery_hints"][0] is not None and error["recovery_hints"][0]["kind"] == (
        "use_lookalike_measure"
    )
    assert hint["audience"] == "caller"
    assert hint["message"] == (
        "Read the same measure from another model in details.lookalike_measures, keeping this "
        f"call's group_by, where and time: '{ATTRIBUTED_REVENUE}' in place of '{REVENUE}' (on "
        "'entity.shop_attributed_order'). Where the call names a result column (order_by, "
        "having), use the row's `columns`. Each model holds its own rows, which may be fewer, "
        "more or other rows (such as every version of each row) than the user asked about: "
        "inspect the measure for its model's description, and tell the user which model the "
        "answer reads."
    )
    # Each look-alike is stated once, in the details; the hint names it by id only.
    assert json.dumps(error).count(ATTRIBUTED) == 1
    # The named call answers, and only for the attributed orders' rows (hand SQL below).
    swapped = _follow(CAMPAIGN_REVENUE, row)
    assert swapped == {"select": [ATTRIBUTED_REVENUE], "group_by": [CAMPAIGN]}
    result = mcp_call(runtime, "execute", {"query": swapped})
    assert result["ok"], result
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        expected = connection.execute(
            "SELECT campaign, SUM(amount) FROM attributed_orders GROUP BY 1 ORDER BY 1"
        ).fetchall()
    assert sorted((row[CAMPAIGN], row["attributed_revenue"]) for row in result["rows"]) == expected


def test_the_rooms_copy_that_reaches_buildings_answers(shop):
    # Room area by building: the history copy has no route to buildings, the legacy copy's area
    # is text (a dimension, no measure), and the current table answers.
    error = _refusal(_validate(shop(), AREA_BY_BUILDING))
    assert error["details"]["lookalike_measures"] == [
        {
            "entity": "entity.shop_room",
            "measures": {HIST_AREA: ROOMS_AREA},
            "description": "One row per room in use.",
            "columns": {"rooms_hist_area": "rooms_area"},
        }
    ]
    assert "room_legacy" not in json.dumps(error["details"]["lookalike_measures"])


def test_a_ranked_call_written_as_the_hint_says_compiles(shop):
    # Top buildings by room area, ordered and filtered by the result column's default name.
    ranked = {
        **AREA_BY_BUILDING,
        "having": [{"field": "rooms_hist_area", "op": "gt", "value": 20}],
        "order_by": [{"field": "rooms_hist_area", "direction": "desc"}],
    }
    runtime = shop()
    (row,) = _refusal(_validate(runtime, ranked))["details"]["lookalike_measures"]
    assert row["columns"] == {"rooms_hist_area": "rooms_area"}
    swapped = _follow(ranked, row)
    assert swapped["order_by"] == [{"field": "rooms_area", "direction": "desc"}]
    result = mcp_call(runtime, "execute", {"query": swapped})
    assert result["ok"], result
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        expected = connection.execute(
            "SELECT b.name, SUM(r.area) FROM rooms r JOIN buildings b USING (building_id) "
            "GROUP BY 1 HAVING SUM(r.area) > 20 ORDER BY 1"
        ).fetchall()
    assert sorted((item[BUILDING_NAME], item["rooms_area"]) for item in result["rows"]) == expected


def test_two_tables_row_counts_are_never_lookalikes(shop):
    # "How many orders per campaign": the orders table's row count is not the attributed orders'
    # row count, and a count of room versions is not a count of rooms.
    runtime = shop(v2=True)
    for measure, entity, call in (
        ("measure.shop.orders_count", "entity.shop_order", {"group_by": [CAMPAIGN]}),
        ("measure.shop.rooms_hist_count", "entity.shop_room_hist", {"group_by": [BUILDING_NAME]}),
    ):
        error = _refusal(_validate(runtime, {"select": [measure], **call}))
        assert "lookalike_measures" not in error["details"], measure
        assert lookalike_swaps(runtime.package_config, [measure], entity) == []
        assert not {"lookalike_measures", "lookalike_notice"} & set(_card(runtime, measure))
    # A count of the same key on two copies of one table counts the same rooms.
    assert lookalike_swaps(
        runtime.package_config, ["measure.shop.rooms_count"], "entity.shop_room"
    ) == [("entity.shop_room_v2", {"measure.shop.rooms_count": "measure.shop.rooms_v2_count"})]


def _copies(root: Path, count: int) -> None:
    """The shop package with ``count`` more copies of the rooms table, each reaching buildings
    and described at a realistic length."""
    files = _files(False)
    seed = [SEED]
    for index in range(count):
        name = f"rooms_c{index:02d}"
        files[f"models/{name}.yml"] = _rooms(
            name,
            f"room_c{index:02d}",
            f"Copy {index} of the rooms table, loaded nightly from the facilities system of "
            f"campus {index}, with one row per room in use, its building and its floor area in "
            "square metres, kept for the planning team's space reports and reconciled against "
            "the registrar's room list at the start of every term.",
            building=True,
            measure=True,
        )
        graph = files["graph.yml"]["graph"]
        graph["entities"][f"room_c{index:02d}"] = {"key": ["room_id"], "model": name}
        graph["relationships"][f"c{index:02d}_building"] = {
            "entities": [f"room_c{index:02d}", "building"],
            "cardinality": "many_to_one",
        }
        seed.append(f"CREATE TABLE {name} AS SELECT * FROM rooms;")
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(body), encoding="utf-8")
    with duckdb.connect(str(root / "shop.duckdb")) as connection:
        connection.execute("\n".join(seed))


def test_many_lookalikes_fit_the_response_budget_each_stated_once(tmp_path):
    _copies(tmp_path, 30)
    runtime = Runtime.from_path(str(tmp_path))
    try:
        response = _validate(runtime, AREA_BY_BUILDING)
        rows = _refusal(response)["details"]["lookalike_measures"]
        assert len(rows) == 31  # the rooms table and its 30 copies
        text = json.dumps(response)
        assert len(text) < max_result_chars()
        # Each copy is stated once: its row in the details, and its id once more in the hint.
        assert text.count("Copy 7 of the rooms table") == 1
        assert text.count("measure.shop.rooms_c07_area") == 2
        card = _card(runtime, HIST_AREA, AREA_BY_BUILDING)
        assert len(card["lookalike_measures"]) == 31
        assert len(json.dumps(card)) < max_result_chars()
    finally:
        runtime.close()


def test_two_models_that_answer_are_both_named_and_none_is_picked(shop):
    error = _refusal(_validate(shop(v2=True), AREA_BY_BUILDING))
    assert [row["measures"] for row in error["details"]["lookalike_measures"]] == [
        {HIST_AREA: ROOMS_AREA},
        {HIST_AREA: V2_AREA},
    ]
    assert error["details"]["start"] == "entity.shop_room_hist"


@pytest.mark.parametrize("mode", ["validate", "sql", "run"])
def test_every_execute_mode_names_the_same_lookalikes(shop, mode):
    response = mcp_call(shop(), "execute", {"query": CAMPAIGN_REVENUE, "mode": mode})
    rows = _refusal(response)["details"]["lookalike_measures"]
    assert [row["measures"] for row in rows] == [{REVENUE: ATTRIBUTED_REVENUE}]


def test_a_windowed_call_reads_the_lookalike_by_its_own_clock(shop):
    query = {**CAMPAIGN_REVENUE, "time": {"grain": "month", "start": "2026-08-01"}}
    (row,) = _refusal(_validate(shop(), query))["details"]["lookalike_measures"]
    assert row["temporal_role"] == "temporal_role.shop_attributed_order_ordered_at"
    assert (
        "by 'temporal_role.shop_attributed_order_ordered_at'"
        in _hint(_refusal(_validate(shop(), query)))["message"]
    )


def test_a_lookalike_that_totals_another_number_is_never_named(shop):
    def in_euros(config):
        measures = [
            replace(row, currency="EUR") if row.id == ATTRIBUTED_REVENUE else row
            for row in config.measures
        ]
        return replace(config, measures=measures)

    error = _refusal(_validate(shop(change=in_euros), CAMPAIGN_REVENUE))
    assert "lookalike_measures" not in error["details"]
    assert ATTRIBUTED_REVENUE not in json.dumps(error)


def test_measures_named_for_different_things_are_never_lookalikes(jaffle_snapshot):
    # Order count and item count, revenue and item revenue: a model's name in a measure's key
    # says what it counts. Only its table's name, as a mirrored package writes it, is a tag.
    config = jaffle_snapshot.config
    assert [
        (measure.id, swaps)
        for measure in config.measures
        if (swaps := lookalike_swaps(config, [measure.id], measure.entity))
    ] == []


# --- Only what this caller can see and read -----------------------------------------------------


def test_a_lookalike_hidden_from_the_caller_is_never_named(shop):
    hide = visibility_policy("hidden", "entity.shop_room")

    def hidden(config):
        return with_policies(config, hide)

    reader = _validate(shop(change=hidden), AREA_BY_BUILDING, policy_context=CALLER)
    author = _validate(shop(change=hidden), AREA_BY_BUILDING)
    without = _validate(
        shop(change=lambda config: absent(with_policies(config, hide), ROOM_OBJECTS)),
        AREA_BY_BUILDING,
        policy_context=CALLER,
    )
    assert _refusal(author)["details"]["lookalike_measures"][0]["entity"] == "entity.shop_room"
    assert "lookalike_measures" not in _refusal(reader)["details"]
    assert ROOMS_AREA not in json.dumps(reader)
    assert "use_lookalike_measure" not in json.dumps(reader)
    # The reader is told exactly what a package without the hidden model would say.
    assert envelope(reader) == envelope(without)


@pytest.mark.parametrize("target", ["entity.shop_room", ROOMS_AREA])
def test_a_lookalike_the_caller_may_not_read_is_never_named(shop, target):
    # An entity-level deny is the common form: it refuses every measure the entity holds.
    def denied(config):
        return with_policies(config, access_policy("deny", target))

    runtime = shop(change=denied)
    reader = _refusal(_validate(runtime, AREA_BY_BUILDING, policy_context=CALLER))
    assert "lookalike_measures" not in reader["details"]
    assert ROOMS_AREA not in json.dumps(reader)
    author = _refusal(_validate(runtime, AREA_BY_BUILDING))
    assert author["details"]["lookalike_measures"][0]["measures"] == {HIST_AREA: ROOMS_AREA}


def test_enriching_again_keeps_only_what_the_view_sees_and_says_it_once(shop):
    hide = visibility_policy("hidden", "entity.shop_room")
    runtime = shop(change=lambda config: with_policies(config, hide))
    with pytest.raises(SemanticLayerError) as raised:
        runtime.compile(AREA_BY_BUILDING)
    author = raised.value
    assert author.details["lookalike_measures"]
    again = enrich_path_not_found(author, runtime.package_config)
    assert str(again) == str(author)
    assert str(again).count("of the same name") == 1
    reader = enrich_path_not_found(author, view_of(runtime.package_config, CALLER))
    issue = exception_issue(reader, stage="validate")
    assert "lookalike_measures" not in issue["details"]
    assert ROOMS_AREA not in json.dumps(issue)
    assert "of the same name" not in issue["message"]


def test_plan_s_own_drafts_offer_no_lookalikes(shop):
    # plan validates its drafts under the caller's identity; their refusals stay as they were.
    runtime = shop()
    report = _validate_query(runtime, Call.from_call(CAMPAIGN_REVENUE, runtime._config.measures))
    (error,) = report["errors"]
    assert error["code"] == "PATH_NOT_FOUND"
    assert "lookalike_measures" not in error["details"]
    assert ATTRIBUTED_REVENUE not in json.dumps(report)


# --- The card of a measure with look-alikes says so ----------------------------------------------


NOTICE_KEYS = ("model_description", "grouping_entities", "lookalike_measures", "lookalike_notice")


def _card(runtime: Runtime, object_id: str, query: dict[str, Any] | None = None, **arguments):
    arguments = {"object_id": object_id, **arguments, **({"query": query} if query else {})}
    return mcp_call(runtime, "inspect", arguments)["card"]


@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
def test_a_measure_card_names_its_lookalikes_with_their_rows(shop, verbosity):
    card = _card(shop(), REVENUE, CAMPAIGN_REVENUE, verbosity=verbosity)
    assert {key: card[key] for key in NOTICE_KEYS} == {
        "model_description": "One row per completed order.",
        "grouping_entities": ["entity.shop_customer"],
        "lookalike_measures": [
            {
                "measure": ATTRIBUTED_REVENUE,
                "entity": "entity.shop_attributed_order",
                "description": ATTRIBUTED,
                "reaches": [CAMPAIGN],
            }
        ],
        "lookalike_notice": LOOKALIKE_NOTICE,
    }


def test_the_card_says_which_of_the_call_s_dimensions_each_lookalike_reaches(shop):
    card = _card(shop(v2=True), HIST_AREA, AREA_BY_BUILDING)
    assert [(row["measure"], row["reaches"]) for row in card["lookalike_measures"]] == [
        (ROOMS_AREA, [BUILDING_NAME]),
        (V2_AREA, [BUILDING_NAME]),
    ]
    assert "grouping_entities" not in card  # the history copy reaches no other entity
    # Without a call, each look-alike is named with its rows; its own card says where it reaches.
    bare = _card(shop(), ROOMS_AREA)
    assert bare["grouping_entities"] == ["entity.shop_building"]
    assert bare["lookalike_measures"] == [
        {
            "measure": HIST_AREA,
            "entity": "entity.shop_room_hist",
            "description": "One row per version of a room, past and present.",
        }
    ]
    assert _card(shop(), HIST_AREA)["lookalike_measures"][0]["measure"] == ROOMS_AREA
    # The legacy copy's area is text: a categorical dimension, which no call totals.
    legacy = _card(shop(), LEGACY_AREA)
    assert (legacy["kind"], legacy["kind_semantics"]) == ("dimension", "categorical")


def test_a_card_without_lookalikes_says_nothing_new(jaffle_snapshot):
    runtime = Runtime.from_path(jaffle_snapshot.source_path)
    try:
        for measure in runtime.package_config.measures:
            for verbosity in ("minimal", "compact"):
                card = _card(runtime, measure.id, verbosity=verbosity)
                assert not set(NOTICE_KEYS) & set(card), measure.id
    finally:
        runtime.close()


def test_a_card_never_names_a_lookalike_hidden_from_the_caller(shop):
    hide = visibility_policy("hidden", "entity.shop_room")
    reader = shop(change=lambda config: with_policies(config, hide))
    without = shop(change=lambda config: absent(with_policies(config, hide), ROOM_OBJECTS))
    seen = _card(reader, HIST_AREA, AREA_BY_BUILDING, policy_context=CALLER)
    assert not set(NOTICE_KEYS) & set(seen)
    assert ROOMS_AREA not in json.dumps(seen)
    # The reader's card is the card of a package without the hidden model.
    assert seen == _card(without, HIST_AREA, AREA_BY_BUILDING, policy_context=CALLER)
    author = _card(reader, HIST_AREA, AREA_BY_BUILDING)
    assert [row["measure"] for row in author["lookalike_measures"]] == [ROOMS_AREA]


@pytest.mark.parametrize("target", ["entity.shop_room", ROOMS_AREA])
def test_a_card_never_names_a_lookalike_the_caller_may_not_read(shop, target):
    runtime = shop(change=lambda config: with_policies(config, access_policy("deny", target)))
    seen = _card(runtime, HIST_AREA, policy_context=CALLER)
    assert not set(NOTICE_KEYS) & set(seen)
    assert ROOMS_AREA not in json.dumps(seen)
    author = _card(runtime, HIST_AREA)
    assert [row["measure"] for row in author["lookalike_measures"]] == [ROOMS_AREA]
