"""Extreme entity-hop correctness: long relationship chains must be either
numerically correct or a structured refusal — never silently wrong.

Fixture: a six-entity geography ladder seeded into DuckDB with
hand-computable ground truth:

    line_item -> order -> customer -> city -> region -> country
       (fact)     N:1       N:1       N:1      N:1       N:1

plus an optional role-playing shortcut (``order.ship_city_id -> city``)
that creates a diamond: the ship-to route and the home-address route reach
the same entities with different semantics. Ground truth differs between
the two routes on purpose, so a test can tell from the *numbers* which
join path the compiler actually took.

These are the behaviors the future acceleration layer depends on: hop
counts must be observable (hop_profile), long chains must be opt-in but
correct (path_policy.max_hops), and route identity must be pinnable
(path_preferences) and conflict-checked (PATH_JOIN_CONFLICT).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime


@pytest.fixture(autouse=True)
def _allow_external_package_paths(monkeypatch):
    # _write_geo_package keeps default_db one level above the package root
    # — the trusted local flow the containment check gates.
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


SEED_SQL = """
CREATE TABLE countries (country_id INTEGER, country_name VARCHAR);
CREATE TABLE regions (region_id INTEGER, country_id INTEGER, region_name VARCHAR);
CREATE TABLE cities (city_id INTEGER, region_id INTEGER, city_name VARCHAR);
CREATE TABLE customers (customer_id INTEGER, city_id INTEGER, customer_name VARCHAR);
CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, ship_city_id INTEGER, channel VARCHAR);
CREATE TABLE line_items (line_item_id INTEGER, order_id INTEGER, amount_cents INTEGER, ordered_at TIMESTAMP);

INSERT INTO countries VALUES (1, 'United States'), (2, 'Canada');
INSERT INTO regions VALUES (10, 1, 'West'), (11, 1, 'East'), (12, 2, 'Quebec');
INSERT INTO cities VALUES (100, 10, 'San Francisco'), (101, 10, 'Los Angeles'), (102, 11, 'New York'), (103, 12, 'Montreal');
INSERT INTO customers VALUES (1000, 100, 'c1'), (1001, 101, 'c2'), (1002, 102, 'c3'), (1003, 103, 'c4'), (1004, 102, 'c5');
INSERT INTO orders VALUES
  (1, 1000, 100, 'web'),
  (2, 1000, 102, 'web'),
  (3, 1001, 101, 'store'),
  (4, 1002, 103, 'web'),
  (5, 1003, 103, 'store'),
  (6, 1004, 102, 'store');
INSERT INTO line_items VALUES
  (1, 1, 1000, TIMESTAMP '2025-01-10 10:00:00'),
  (2, 1, 2000, TIMESTAMP '2025-01-10 10:00:00'),
  (3, 2, 3000, TIMESTAMP '2025-02-11 11:00:00'),
  (4, 3, 4000, TIMESTAMP '2025-03-12 12:00:00'),
  (5, 4, 500, TIMESTAMP '2025-04-13 13:00:00'),
  (6, 4, 1500, TIMESTAMP '2025-04-13 13:00:00'),
  (7, 5, 7000, TIMESTAMP '2025-05-14 14:00:00'),
  (8, 6, 2500, TIMESTAMP '2025-06-15 15:00:00')
"""

# Revenue grouped by the customer's HOME region (the 4-hop declared route).
REVENUE_BY_REGION_HOME = {"East": 45.0, "Quebec": 70.0, "West": 100.0}
# Revenue grouped by the order's SHIP-TO region (the 3-hop shortcut route).
REVENUE_BY_REGION_SHIP = {"East": 55.0, "Quebec": 90.0, "West": 70.0}
REVENUE_BY_COUNTRY_HOME = {"Canada": 70.0, "United States": 145.0}
CUSTOMERS_BY_CHANNEL = {"store": 3, "web": 2}

CUSTOMER_ROUTE_TO_REGION = [
    "relationship.line_items_order",
    "relationship.orders_customer",
    "relationship.customers_city",
    "relationship.cities_region",
]


MODELS = {
    "line_items": """
        entities: {line_item: {}, order: {}}
        times:
          ordered_at: {label: Ordered at, column: ordered_at, kind: timestamp, class: event_time,
            as: temporal_role.geo_order_time, default: true}
        measures:
          revenue_usd: {kind: aggregate, label: Revenue, expr: amount_cents / 100.0,
            accumulation: {kind: flow}, value_type: currency, currency: USD}
        """,
    "orders": """
        entities: {order: {}, customer: {}}
        dimensions:
          channel: {as: dimension.geo_channel, label: Channel, kind: categorical}
        """,
    "customers": """
        entities: {customer: {}, city: {}}
        dimensions:
          customer_name: {as: dimension.geo_customer_name, label: Customer name, kind: categorical}
        measures:
          customer_count: {kind: entity_count, label: Customers, entity_key: customer_id,
            accumulation: {kind: population}, value_type: count}
        """,
    "cities": """
        entities: {city: {}, region: {}}
        dimensions:
          city_name: {as: dimension.geo_city_name, label: City name, kind: categorical}
        """,
    "regions": """
        entities: {region: {}, country: {}}
        dimensions:
          region_name: {as: dimension.geo_region_name, label: Region name, kind: categorical}
        """,
    "countries": """
        entities: {country: {}}
        dimensions:
          country_name: {as: dimension.geo_country_name, label: Country name, kind: categorical}
        """,
}
ENTITIES = {
    "line_item": ("line_item_id", "line_items"),
    "order": ("order_id", "orders"),
    "customer": ("customer_id", "customers"),
    "city": ("city_id", "cities"),
    "region": ("region_id", "regions"),
    "country": ("country_id", "countries"),
}


def _write_geo_package(
    root: Path,
    *,
    ship_city: bool = False,
    rollup_safe_reverse: bool = False,
    path_preferences: list[dict] | None = None,
    max_hops: int | None = None,
) -> Path:
    pkg = root / "geo_chain"
    package = {
        "schema_version": 1,
        "package": {
            "id": "geo_chain",
            "namespace": "geo",
            "name": "geo_chain",
            "warehouse": "duckdb",
            "default_db": (root / "geo_chain.duckdb").as_posix(),
            "seed": {"kind": "sql_script", "source": "data/seed.sql"},
        },
        "defaults": {
            "dimension": {"groupable": True, "filterable": True},
            "relationship": {"traversal": ["forward", "reverse"]},
        },
    }
    graph: dict = {
        "entities": {
            name: {"label": name, "key": [key], "model": model}
            for name, (key, model) in ENTITIES.items()
        },
        "relationships": {},
    }
    if ship_city:
        graph["relationships"]["orders_ship_city"] = {
            "as": "relationship.orders_ship_city",
            "entities": ["order", "city"],
            "cardinality": "many_to_one",
            "via": ["ship_city_id"],
            "target": ["city_id"],
            "allowed_directions": ["forward"],
        }
    if rollup_safe_reverse:
        graph["relationships"]["orders_customer"] = {
            "as": "relationship.orders_customer",
            "entities": ["order", "customer"],
            "cardinality": "many_to_one",
            "rollup_safe": {"reverse": ["count_distinct"]},
        }
    if max_hops is not None:
        graph["path_policy"] = {"max_hops": max_hops}
    if path_preferences:
        graph["path_preferences"] = [
            {
                "source_entity": pref["source"],
                "target_entity": pref["target"],
                "preferred_paths": [pref["path"]],
            }
            for pref in path_preferences
        ]
    files = {
        "data/seed.sql": SEED_SQL,
        "package.yml": yaml.safe_dump(package),
        "graph.yml": yaml.safe_dump({"graph": graph}),
        **{
            f"models/{name}.yml": f"model:\n  id: {name}\n  relation: {name}\n"
            + textwrap.indent(textwrap.dedent(body), "  ")
            for name, body in MODELS.items()
        },
    }
    for name, text in files.items():
        (pkg / name).parent.mkdir(parents=True, exist_ok=True)
        (pkg / name).write_text(text)
    return pkg


def _ask(tmp_path: Path, measure: str, *group_by: str, **package) -> dict:
    """The measure grouped by the named geo dimensions, values keyed by the group."""
    runtime = Runtime.from_path(str(_write_geo_package(tmp_path, **package)))
    dimensions = [f"dimension.geo_{name}" for name in group_by]
    query = {
        "select": [{"expression": f"measure.geo.{measure}", "as": "value"}],
        "group_by": dimensions,
        "order_by": [{"field": dimensions[0], "direction": "asc"}],
        "limit": 50,
    }
    try:
        out = runtime.query(query)
    finally:
        runtime.close()
    groups = [tuple(row[name] for name in dimensions) for row in out["rows"]]
    out["values"] = {
        group if len(group) > 1 else group[0]: float(row["value"])
        for group, row in zip(groups, out["rows"], strict=True)
    }
    return out


def _refused(tmp_path: Path, measure: str, *group_by: str, **package) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as exc_info:
        _ask(tmp_path, measure, *group_by, **package)
    return exc_info.value


def test_four_hop_chain_executes_with_correct_numbers(tmp_path):
    out = _ask(tmp_path, "revenue_usd", "region_name")

    assert out["values"] == REVENUE_BY_REGION_HOME
    profile = out["hop_profile"]
    assert profile["root_entity"] == "entity.geo_line_item"
    assert profile["max_hop_count"] == 4
    assert profile["long_hop_targets"] == ["entity.geo_region"]
    target = profile["targets"]["entity.geo_region"]
    assert target["hop_count"] == 4
    assert target["path"] == CUSTOMER_ROUTE_TO_REGION
    assert [hop["direction"] for hop in target["hops"]] == ["forward"] * 4


def test_five_hops_beyond_limit_is_a_structured_refusal(tmp_path):
    err = _refused(tmp_path, "revenue_usd", "country_name")

    assert err.code == "PATH_NOT_FOUND"
    assert err.details["reason"] == "hop_limit_exceeded"
    assert err.details["reachable_at_hops"] == 5
    assert err.details["hop_limit"] == 4
    assert "max_hops" in err.details["hint"]


def test_five_hop_chain_executes_when_package_raises_max_hops(tmp_path):
    out = _ask(tmp_path, "revenue_usd", "country_name", max_hops=6)

    assert out["values"] == REVENUE_BY_COUNTRY_HOME
    assert out["hop_profile"]["max_hop_count"] == 5
    assert out["hop_profile"]["hop_limit"] == 6


def test_reverse_fanout_count_distinct_rolls_up_by_child_dimension(tmp_path):
    """'Entity measure in terms of another entity' across a reverse hop:
    distinct customers grouped by a dimension of their child orders."""
    out = _ask(tmp_path, "customer_count", "channel", rollup_safe_reverse=True)

    assert out["values"] == CUSTOMERS_BY_CHANNEL
    assert any(w["code"] == "REWRITE_APPLIED" for w in out["warnings"])


SHIP_ROUTE_TO_REGION = [
    "relationship.line_items_order",
    "relationship.orders_ship_city",
    "relationship.cities_region",
]


def test_conflicting_routes_to_one_table_are_refused_not_silently_wrong(tmp_path):
    """A customer's city is its own key, and its region is recorded as the regions its orders
    ship to: both need the `cities` table through different relationships. One table instance
    cannot serve both semantics. The count of customers across the orders' fan-out is computed
    from the orders, which must read the customer's city, not the order's ship-to city."""
    ship_to = ["relationship.orders_customer", *SHIP_ROUTE_TO_REGION[1:]]
    err = _refused(
        tmp_path,
        "customer_count",
        "region_name",
        "city_name",
        ship_city=True,
        rollup_safe_reverse=True,
        path_preferences=[{"source": "customer", "target": "region", "path": ship_to}],
    )
    assert err.code == "PATH_JOIN_CONFLICT"
    assert err.details["table"] == "cities"
    assert {err.details["existing_relationship"], err.details["conflicting_relationship"]} == {
        "relationship.customers_city",
        "relationship.orders_ship_city",
    }


def test_consistent_pins_resolve_the_conflict(tmp_path):
    """Pinning BOTH region and city to the customer-home route makes the
    same query legal again — and the numbers follow the pinned route."""
    pins = [
        {"source": "line_item", "target": "region", "path": CUSTOMER_ROUTE_TO_REGION},
        {"source": "line_item", "target": "city", "path": CUSTOMER_ROUTE_TO_REGION[:3]},
    ]
    out = _ask(
        tmp_path, "revenue_usd", "region_name", "city_name", ship_city=True, path_preferences=pins
    )
    assert out["values"] == {
        ("East", "New York"): 45.0,
        ("Quebec", "Montreal"): 70.0,
        ("West", "Los Angeles"): 40.0,
        ("West", "San Francisco"): 60.0,
    }


def _pin_region(path: list[str]) -> dict:
    return {"path_preferences": [{"source": "line_item", "target": "region", "path": path}]}


# Region recorded as the customer-home route walks line item -> city, which a second row records
# as the ship-to shortcut: two definitions of one pair, refused when the package loads, naming
# both rows, before any query can need the `cities` table both ways.
TWO_ROUTES_THROUGH_ONE_PAIR = [
    {"source": "line_item", "target": "region", "path": CUSTOMER_ROUTE_TO_REGION},
    {"source": "line_item", "target": "city", "path": SHIP_ROUTE_TO_REGION[:2]},
]


@pytest.mark.parametrize(
    ("package", "words", "rows"),
    [
        pytest.param({"max_hops": 99}, "max_hops", None, id="max-hops-out-of-range"),
        pytest.param(
            _pin_region(["relationship.does_not_exist"]),
            "does_not_exist",
            None,
            id="unknown-relationship",
        ),
        # Stops at customer, never reaching region.
        pytest.param(_pin_region(CUSTOMER_ROUTE_TO_REGION[:2]), "ends at", None, id="wrong-target"),
        pytest.param(
            {"ship_city": True, "path_preferences": TWO_ROUTES_THROUGH_ONE_PAIR},
            "",
            [CUSTOMER_ROUTE_TO_REGION, SHIP_ROUTE_TO_REGION[:2]],
            id="two-routes-through-one-pair",
        ),
    ],
)
def test_bad_path_settings_fail_at_load(tmp_path, package, words, rows):
    with pytest.raises(SemanticLayerError) as exc_info:
        Runtime.from_path(str(_write_geo_package(tmp_path, **package)))
    err = exc_info.value
    assert err.code == "INVALID_CONFIG"
    assert words in str(err)
    if rows:
        assert [row["relationship_path"] for row in err.details["rows"]] == rows
