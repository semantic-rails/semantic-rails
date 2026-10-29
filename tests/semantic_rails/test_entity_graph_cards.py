"""An entity's card shows its place in the join graph.

``inspect`` of an entity lists each relationship it takes part in, seen from its side: the
other entity, the cardinality (this entity's rows to the other's), the ways a route may walk it
(``to`` the other entity, ``from`` it) and the join columns as (this, other) pairs. It also lists
the entities a query rooted on it reaches within the hop ceiling, nearest first: ``via`` the
package's route, marked ``recorded`` when recorded route rows chose that route among the
caller's routes, or ``route_decision_required`` when the question has to choose one. A minimal
card keeps the first entries of each list without the routes; compact and full keep both lists
whole. A discover entity row carries its relationships at verbosity full.

Everything reads the caller's visible view: a hidden relationship or entity is never listed,
and an entity whose package route reads a hidden object is not reachable for that caller.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata_parts import entity_graph
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PackageConfig, RelationshipConfig, SemanticPolicyConfig
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.hidden_absent import (
    ACTIONS,
    CALLER,
    ELIGIBLE,
    absent,
    envelope,
    hidden_tokens,
    leaks,
    mcp_call,
    visibility_policy,
    with_policies,
)

CUSTOMER = "entity.jaffle_customer"
HISTORY = "entity.jaffle_customer_history"
ORDER = "entity.jaffle_order"
STORE = "entity.jaffle_store"
PRODUCT = "entity.jaffle_product"
SUPPLY = "entity.jaffle_supply"
ORDERS_CUSTOMER = "relationship.orders_customer"
ORDERS_STORE = "relationship.orders_store"
ITEMS_ORDER = "relationship.order_items_order"


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PackageConfig]:
    root = copy_package_config(tmp_path_factory.mktemp("graph"), "jaffle_shop", preseed_db=True)
    return root, load_package_config(str(root))


@pytest.fixture(scope="module")
def runtimes(package: tuple[Path, PackageConfig]) -> Iterator[Callable[..., Runtime]]:
    """Runtimes over the package with extra policies, cached; ``hide`` makes the absent one."""
    root, config = package
    made: dict[str, Runtime] = {}

    def runtime(*policies: SemanticPolicyConfig, hide: frozenset[str] | None = None) -> Runtime:
        key = repr((policies, sorted(hide) if hide is not None else None))
        if key not in made:
            governed = with_policies(config, *policies)
            if hide is not None:
                governed = absent(governed, hide)
            made[key] = Runtime.from_config(governed, source_path=str(root))
        return made[key]

    yield runtime
    for value in made.values():
        value.close()


@pytest.fixture(scope="module")
def adapter(runtimes: Callable[..., Runtime]) -> Iterator[SemanticLayerMCPAdapter]:
    adapter = SemanticLayerMCPAdapter(runtimes())
    yield adapter
    adapter.close()


def _card(adapter: SemanticLayerMCPAdapter, object_id: str, verbosity: str) -> dict[str, Any]:
    out = adapter.call_tool("inspect", {"object_id": object_id, "verbosity": verbosity})
    assert out["ok"] is True, out
    return out["card"]


def test_an_entity_card_lists_its_relationships_from_its_own_side(adapter):
    card = _card(adapter, HISTORY, "compact")
    assert card["relationships"] == [
        {
            "id": "relationship.jaffle_order_customer_history",
            "entity": ORDER,
            # Many orders to one history row, walked only from the order.
            "cardinality": "1:N",
            "directions": ["from"],
            "on": [["customer_id", "customer_id"]],
            "temporal_validity": {
                "valid_from": "jaffle_customer_history.valid_from",
                "valid_to": "jaffle_customer_history.valid_to",
            },
        },
        {
            "id": "relationship.customer_history_customer",
            "entity": CUSTOMER,
            "cardinality": "N:1",
            "directions": ["to"],
            "on": [["customer_id", "customer_id"]],
        },
        {
            "id": "relationship.customer_history_store",
            "entity": STORE,
            "cardinality": "N:1",
            "directions": ["to"],
            "on": [["preferred_store_id", "store_id"]],
        },
    ]


def test_both_directions_and_a_self_relationship_read_from_the_source_side():
    both = RelationshipConfig(
        id="relationship.a_b",
        source_entity="entity.a",
        target_entity="entity.b",
        source_column="b_id",
        target_column="id",
        cardinality="N:1",
        safety="safe",
    )
    assert entity_graph.relationship_from(both, "entity.b") == {
        "id": "relationship.a_b",
        "entity": "entity.a",
        "cardinality": "1:N",
        "directions": ["to", "from"],
        "on": [["id", "b_id"]],
    }
    manager = replace(
        both,
        id="relationship.manager",
        target_entity="entity.a",
        source_column="manager_id",
        allowed_directions=["forward"],
    )
    assert entity_graph.relationship_from(manager, "entity.a") == {
        "id": "relationship.manager",
        "entity": "entity.a",
        "cardinality": "N:1",
        "directions": ["to"],
        "on": [["manager_id", "id"]],
    }
    many = replace(both, cardinality="M:N")
    assert entity_graph.relationship_from(many, "entity.b")["cardinality"] == "M:N"


def test_reachable_entities_follow_the_package_routes(adapter):
    reachable = _card(adapter, CUSTOMER, "compact")["reachable_entities"]
    by_id = {row["id"]: row for row in reachable}
    # Its own key's relationship is the only route to a session.
    assert by_id["entity.jaffle_storefront_session"] == {
        "id": "entity.jaffle_storefront_session",
        "via": ["relationship.storefront_sessions_customer"],
    }
    # graph.path_preferences records the route to a store.
    assert by_id[STORE] == {"id": STORE, "via": [ORDERS_CUSTOMER, ORDERS_STORE], "recorded": True}
    # The routes to an item inherit that recorded row.
    assert by_id["entity.jaffle_item"] == {
        "id": "entity.jaffle_item",
        "via": [ORDERS_CUSTOMER, ITEMS_ORDER],
        "recorded": True,
    }
    # An order is reached through its own orders or through sessions: the question chooses
    # between 2 routes, each a meaning of its own (a group).
    assert by_id[ORDER] == {"id": ORDER, "route_decision_required": True, "routes": 2, "groups": 2}
    # Only relationships a route may walk count: a lifecycle row looks a customer up, never
    # the reverse, and the calendars are not related at all.
    assert CUSTOMER not in by_id
    assert "entity.jaffle_order_lifecycle" not in by_id
    assert "entity.jaffle_fiscal_calendar" not in by_id
    # Nearest first: the entities one relationship away lead, and no route passes the ceiling.
    assert {row["id"] for row in reachable[:2]} == {ORDER, "entity.jaffle_storefront_session"}
    assert max(len(row["via"]) for row in reachable if "via" in row) <= 4


def test_a_minimal_card_keeps_the_first_entries_without_routes(adapter, monkeypatch):
    full = _card(adapter, CUSTOMER, "compact")
    minimal = _card(adapter, CUSTOMER, "minimal")
    assert minimal["relationships"] == full["relationships"]
    assert minimal["reachable_entities"] == [
        {key: value for key, value in row.items() if key != "via"}
        for row in full["reachable_entities"]
    ]
    assert "more_relationships" not in minimal and "more_reachable_entities" not in minimal

    monkeypatch.setattr(entity_graph, "MINIMAL_LIST_CAP", 2)
    capped = _card(adapter, CUSTOMER, "minimal")
    assert capped["relationships"] == full["relationships"][:2]
    assert capped["more_relationships"] == len(full["relationships"]) - 2
    assert [row["id"] for row in capped["reachable_entities"]] == [
        row["id"] for row in full["reachable_entities"][:2]
    ]
    assert capped["more_reachable_entities"] == len(full["reachable_entities"]) - 2
    assert _card(adapter, CUSTOMER, "full")["reachable_entities"] == full["reachable_entities"]


def test_an_unrelated_entity_card_has_no_graph_lists(adapter):
    card = _card(adapter, "entity.jaffle_fiscal_calendar", "compact")
    assert card["relationships"] == [] and card["reachable_entities"] == []
    assert "relationships" not in _card(adapter, "entity.jaffle_fiscal_calendar", "minimal")


def test_a_discover_entity_row_carries_its_relationships_at_full_verbosity(adapter):
    full = adapter.call_tool(
        "discover", {"terms": "store", "kinds": ["entity"], "verbosity": "full"}
    )
    (row,) = [row for row in full["entities"] if row["id"] == STORE]
    assert row["relationships"] == _card(adapter, STORE, "compact")["relationships"]
    # Reachability follows the package's routes: inspect answers it.
    assert "reachable_entities" not in row
    minimal = adapter.call_tool("discover", {"terms": "store", "kinds": ["entity"]})
    (slim,) = [row for row in minimal["entities"] if row["id"] == STORE]
    assert "relationships" not in slim and "reachable_entities" not in slim


# A hidden entity and an entity it relates to. The history entity's relationships are other
# routes from an item, which recorded rows rule out: the caller's card must not say so.
HIDDEN_NEIGHBOURS = [
    (SUPPLY, PRODUCT, "relationship.supplies_product"),
    (HISTORY, "entity.jaffle_item", "relationship.jaffle_order_customer_history"),
]


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize(("target", "inspected", "relationship"), HIDDEN_NEIGHBOURS)
def test_a_hidden_entity_and_its_relationships_never_appear(
    runtimes, action, target, inspected, relationship
):
    governed = runtimes(visibility_policy(action, target))
    hidden = _hidden(governed.package_config)
    assert {target, relationship} <= hidden
    missing = runtimes(hide=hidden)
    tokens = hidden_tokens(governed.package_config, hidden)
    for verbosity in ("minimal", "compact", "full"):
        request = {"object_id": inspected, "verbosity": verbosity, "policy_context": CALLER}
        response = mcp_call(governed, "inspect", request)
        assert response["ok"] is True, response
        assert not leaks(response, tokens, request=request)
        assert envelope(response) == envelope(mcp_call(missing, "inspect", request))
    for kinds in (["relationship"], ["entity"]):
        request = {
            "terms": inspected.removeprefix("entity.jaffle_"),
            "kinds": kinds,
            "verbosity": "full",
            "policy_context": CALLER,
        }
        response = mcp_call(governed, "discover", request)
        assert response["ok"] is True, response
        assert not leaks(response, tokens, request=request)
        assert envelope(response) == envelope(mcp_call(missing, "discover", request))


@pytest.mark.parametrize("action", ACTIONS)
def test_a_route_through_a_hidden_relationship_is_never_rechosen(runtimes, action):
    governed = runtimes(visibility_policy(action, ORDERS_STORE))
    hidden = _hidden(governed.package_config)
    tokens = hidden_tokens(governed.package_config, hidden)
    request = {"object_id": ORDER, "verbosity": "full", "policy_context": CALLER}
    response = mcp_call(governed, "inspect", request)
    assert response["ok"] is True, response
    assert not leaks(response, tokens, request=request)
    card = response["card"]
    assert ORDERS_STORE not in {row["id"] for row in card["relationships"]}
    # The package reaches a store by the order's own key, which the caller cannot see; the
    # route through the customer's history is another question, never offered in its place.
    assert STORE not in {row["id"] for row in card["reachable_entities"]}
    assert CUSTOMER in {row["id"] for row in card["reachable_entities"]}
    # A caller who sees the relationship still reaches the store.
    eligible = {"object_id": ORDER, "verbosity": "full", "policy_context": ELIGIBLE}
    plain = mcp_call(governed, "inspect", eligible)
    assert STORE in {row["id"] for row in plain["card"]["reachable_entities"]}


def _hidden(config: PackageConfig) -> set[str]:
    from semantic_rails.request_context import context_from_policy_context
    from semantic_rails.visible_view import hidden_object_ids

    caller = context_from_policy_context(CALLER)
    return set(
        hidden_object_ids(
            config, environment=caller.environment, audience=caller.audience, roles=caller.roles
        )
    )
