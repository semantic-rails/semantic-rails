"""A ``PATH_NOT_FOUND`` names the same measure on another model when that one answers the call.

A call that groups or filters a measure by a dimension its model has no route to is refused.
When another model holds the same measure (the same key or label, each without its model's own
tag, totalled the same way) and the call compiles with it in place, for this caller, under this
caller's policies, the refusal names it by id with what its model says its rows are. The agent
picks; the engine never answers from a model the call didn't name. A measure the caller can't
see or may not read is never named, and ``plan`` validating its own drafts offers none.
A measure's full inspect card lists the entities a call reading it can group by.
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
from semantic_rails.planner import plan_payload
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


def _orders(
    model_id: str, entity: str, description: str, dimension: str, key: str, customer: bool
):
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
            "measures": {key: _measure("Revenue", "amount", "currency")},
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
            "measures": {f"{model_id}_area": _measure(f"Area ({model_id.upper()})", "area")}
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


CAMPAIGN_REVENUE = {"select": [REVENUE], "group_by": [CAMPAIGN]}
# The rooms model's objects, which hiding its entity hides with it.
ROOM_OBJECTS = [
    "entity.shop_room",
    ROOMS_AREA,
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
    }
    assert error["details"]["lookalike_measures"] == [row]
    assert error["message"].endswith(
        "The same-named measures of another model answer this call as it stands: "
        f"'{ATTRIBUTED_REVENUE}' in place of '{REVENUE}' (on 'entity.shop_attributed_order')."
    )
    hint = _hint(error)
    assert error["recovery_hints"][0] is not None and error["recovery_hints"][0]["kind"] == (
        "use_lookalike_measure"
    )
    assert hint["audience"] == "caller"
    assert hint["lookalike_measures"] == [row]
    assert hint["message"] == (
        "Read the same measure from another model, keeping this call's group_by, where and "
        f"time. '{ATTRIBUTED_REVENUE}' in place of '{REVENUE}' (on 'entity.shop_attributed_order'): "
        "Only orders attributed to a marketing campaign; unattributed orders are not in it. "
        "Each model holds its own rows, which may not be every row the user asked about: tell "
        "the user which model the answer reads."
    )
    # The named call answers, and only for the attributed orders' rows (hand SQL below).
    swapped = {"select": [ATTRIBUTED_REVENUE], "group_by": [CAMPAIGN]}
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
        }
    ]
    assert "room_legacy" not in json.dumps(error["details"]["lookalike_measures"])


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
    assert "by 'temporal_role.shop_attributed_order_ordered_at'" in _hint(
        _refusal(_validate(shop(), query))
    )["message"]


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
    assert str(again).count("same-named measures") == 1
    reader = enrich_path_not_found(author, view_of(runtime.package_config, CALLER))
    issue = exception_issue(reader, stage="validate")
    assert "lookalike_measures" not in issue["details"]
    assert ROOMS_AREA not in json.dumps(issue)
    assert "same-named" not in issue["message"]


def test_plan_s_own_drafts_offer_no_lookalikes(shop):
    # plan validates its drafts with the caller's identity; their refusals stay as they were.
    runtime = shop()
    payload = plan_payload(
        runtime,
        intent="revenue by campaign",
        partial_query={"select": [REVENUE], "group_by": [CAMPAIGN], "policy_context": {}},
    )
    assert payload["status"] != "ok"
    assert "lookalike" not in json.dumps(payload)


# --- The card says where a measure reaches -------------------------------------------------------


def test_a_full_measure_card_lists_the_entities_it_reaches(shop):
    runtime = shop()

    def card(object_id: str, verbosity: str) -> dict[str, Any]:
        arguments = {"object_id": object_id, "verbosity": verbosity}
        return mcp_call(runtime, "inspect", arguments)["card"]

    assert card(HIST_AREA, "full")["grouping_entities"] == []
    assert card(ROOMS_AREA, "full")["grouping_entities"] == ["entity.shop_building"]
    assert card(REVENUE, "full")["grouping_entities"] == ["entity.shop_customer"]
    # The default and compact cards stay as they were.
    assert "grouping_entities" not in card(ROOMS_AREA, "minimal")
    assert "grouping_entities" not in card(ROOMS_AREA, "compact")
    # The legacy copy's area is text: a categorical dimension, which no call totals.
    legacy = card(LEGACY_AREA, "minimal")
    assert (legacy["kind"], legacy["kind_semantics"]) == ("dimension", "categorical")


def test_a_full_measure_card_lists_only_what_the_caller_sees(shop):
    hide = visibility_policy("hidden", "entity.shop_building")
    runtime = shop(change=lambda config: with_policies(config, hide))
    arguments = {"object_id": ROOMS_AREA, "verbosity": "full", "policy_context": CALLER}
    assert mcp_call(runtime, "inspect", arguments)["card"]["grouping_entities"] == []
