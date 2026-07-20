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

import json
import textwrap
from pathlib import Path

import pytest

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


def _write_geo_package(
    root: Path,
    *,
    ship_city: bool = False,
    ship_city_preference: int | None = None,
    rollup_safe_reverse: bool = False,
    path_preferences: list[dict] | None = None,
    pref_key: str = "preferred_paths",
    max_hops: int | None = None,
) -> Path:
    pkg = root / "geo_chain"
    (pkg / "data").mkdir(parents=True, exist_ok=True)
    (pkg / "models").mkdir(exist_ok=True)
    (pkg / "data" / "seed.sql").write_text(SEED_SQL)

    (pkg / "package.yml").write_text(
        textwrap.dedent(
            f"""
            schema_version: 1
            package:
              id: geo_chain
              namespace: geo
              name: geo_chain
              description: Deep entity-chain fixture for extreme-hop tests.
              warehouse: duckdb
              default_db: {(root / "geo_chain.duckdb").as_posix()}
              seed:
                kind: sql_script
                source: data/seed.sql
            defaults:
              dimension:
                groupable: true
                filterable: true
              measure:
                subject_entity: self
                aggregation_entity: self
              relationship:
                traversal: [forward, reverse]
            """
        )
    )

    graph = textwrap.dedent(
        """\
        graph:
          entities:
            line_item: {label: Line item, key: [line_item_id], model: line_items}
            order: {label: Order, key: [order_id], model: orders}
            customer: {label: Customer, key: [customer_id], model: customers}
            city: {label: City, key: [city_id], model: cities}
            region: {label: Region, key: [region_id], model: regions}
            country: {label: Country, key: [country_id], model: countries}
        """
    )
    rel_lines: list[str] = []
    if ship_city:
        rel_lines += [
            "    orders_ship_city:",
            "      id: relationship.orders_ship_city",
            "      entities: [order, city]",
            "      cardinality: many_to_one",
            "      via: [ship_city_id]",
            "      target: [city_id]",
            "      allowed_directions: [forward]",
        ]
        if ship_city_preference is not None:
            rel_lines.append(f"      path_preference: {ship_city_preference}")
    if rollup_safe_reverse:
        rel_lines += [
            "    orders_customer:",
            "      id: relationship.orders_customer",
            "      entities: [order, customer]",
            "      cardinality: many_to_one",
            "      rollup_safe:",
            "        reverse: [count_distinct]",
        ]
    if rel_lines:
        graph += "  relationships:\n" + "\n".join(rel_lines) + "\n"
    if max_hops is not None:
        graph += f"  path_policy:\n    max_hops: {max_hops}\n"
    if path_preferences:
        graph += "  path_preferences:\n"
        for pref in path_preferences:
            path_value = (
                json.dumps([pref["path"]])
                if pref_key == "preferred_paths"
                else json.dumps(pref["path"])
            )
            graph += (
                f"    - source_entity: {pref['source']}\n"
                f"      target_entity: {pref['target']}\n"
                f"      {pref_key}: {path_value}\n"
            )
    (pkg / "graph.yml").write_text(graph)

    (pkg / "models" / "line_items.yml").write_text(
        textwrap.dedent(
            """
            model:
              id: line_items
              relation: line_items
              entities:
                line_item: {}
                order: {}
              times:
                ordered_at:
                  label: Ordered at
                  column: ordered_at
                  kind: timestamp
                  class: event_time
                  as: temporal_role.geo_order_time
                  default: true
                  default_query_axis: true
              measures:
                revenue_usd:
                  kind: aggregate
                  label: Revenue
                  expr: amount_cents / 100.0
                  accumulation: {kind: flow}
                  value_type: currency
                  currency: USD
            """
        )
    )
    (pkg / "models" / "orders.yml").write_text(
        textwrap.dedent(
            """
            model:
              id: orders
              relation: orders
              entities:
                order: {}
                customer: {}
              dimensions:
                channel:
                  as: dimension.geo_channel
                  label: Channel
                  kind: categorical
            """
        )
    )
    (pkg / "models" / "customers.yml").write_text(
        textwrap.dedent(
            """
            model:
              id: customers
              relation: customers
              entities:
                customer: {}
                city: {}
              dimensions:
                customer_name:
                  as: dimension.geo_customer_name
                  label: Customer name
                  kind: categorical
              measures:
                customer_count:
                  kind: entity_count
                  label: Customers
                  entity_key: customer_id
                  accumulation: {kind: population}
                  value_type: count
            """
        )
    )
    (pkg / "models" / "cities.yml").write_text(
        textwrap.dedent(
            """
            model:
              id: cities
              relation: cities
              entities:
                city: {}
                region: {}
              dimensions:
                city_name:
                  as: dimension.geo_city_name
                  label: City name
                  kind: categorical
            """
        )
    )
    (pkg / "models" / "regions.yml").write_text(
        textwrap.dedent(
            """
            model:
              id: regions
              relation: regions
              entities:
                region: {}
                country: {}
              dimensions:
                region_name:
                  as: dimension.geo_region_name
                  label: Region name
                  kind: categorical
            """
        )
    )
    (pkg / "models" / "countries.yml").write_text(
        textwrap.dedent(
            """
            model:
              id: countries
              relation: countries
              entities:
                country: {}
              dimensions:
                country_name:
                  as: dimension.geo_country_name
                  label: Country name
                  kind: categorical
            """
        )
    )
    return pkg


def _query(measure: str, group_by: list[str]) -> dict:
    return {
        "version": 1,
        "select": [{"expression": {"measure": measure}, "as": "value"}],
        "group_by": group_by,
        "order_by": [{"field": group_by[0], "direction": "ASC"}],
        "limit": 50,
    }


def _values_by_key(rows: list[dict], key: str) -> dict[str, float]:
    return {row[key]: float(row["value"]) for row in rows}


def test_four_hop_chain_executes_with_correct_numbers(tmp_path):
    runtime = Runtime.from_path(str(_write_geo_package(tmp_path)))
    out = runtime.query(_query("measure.geo.revenue_usd", ["dimension.geo_region_name"]))

    assert _values_by_key(out["rows"], "dimension.geo_region_name") == REVENUE_BY_REGION_HOME

    profile = out["hop_profile"]
    assert profile["root_entity"] == "entity.geo_line_item"
    assert profile["max_hop_count"] == 4
    assert profile["long_hop_targets"] == ["entity.geo_region"]
    target = profile["targets"]["entity.geo_region"]
    assert target["hop_count"] == 4
    assert target["path"] == CUSTOMER_ROUTE_TO_REGION
    assert [hop["direction"] for hop in target["hops"]] == ["forward"] * 4


def test_five_hops_beyond_limit_is_a_structured_refusal(tmp_path):
    runtime = Runtime.from_path(str(_write_geo_package(tmp_path)))
    with pytest.raises(SemanticLayerError) as exc_info:
        runtime.query(_query("measure.geo.revenue_usd", ["dimension.geo_country_name"]))

    err = exc_info.value
    assert err.code == "PATH_NOT_FOUND"
    assert err.details["reason"] == "hop_limit_exceeded"
    assert err.details["reachable_at_hops"] == 5
    assert err.details["hop_limit"] == 4
    assert "max_hops" in err.details["hint"]


def test_five_hop_chain_executes_when_package_raises_max_hops(tmp_path):
    runtime = Runtime.from_path(str(_write_geo_package(tmp_path, max_hops=6)))
    out = runtime.query(_query("measure.geo.revenue_usd", ["dimension.geo_country_name"]))

    assert _values_by_key(out["rows"], "dimension.geo_country_name") == REVENUE_BY_COUNTRY_HOME
    assert out["hop_profile"]["max_hop_count"] == 5
    assert out["hop_profile"]["hop_limit"] == 6


def test_max_hops_outside_supported_range_is_invalid_config(tmp_path):
    with pytest.raises(SemanticLayerError) as exc_info:
        Runtime.from_path(str(_write_geo_package(tmp_path, max_hops=99)))
    assert exc_info.value.code == "INVALID_CONFIG"


def test_reverse_fanout_count_distinct_rolls_up_by_child_dimension(tmp_path):
    """'Entity measure in terms of another entity' across a reverse hop:
    distinct customers grouped by a dimension of their child orders."""
    runtime = Runtime.from_path(str(_write_geo_package(tmp_path, rollup_safe_reverse=True)))
    out = runtime.query(_query("measure.geo.customer_count", ["dimension.geo_channel"]))

    assert _values_by_key(out["rows"], "dimension.geo_channel") == CUSTOMERS_BY_CHANNEL
    assert any(w["code"] == "REWRITE_APPLIED" for w in out["warnings"])


def test_shortcut_relationship_changes_route_and_warns_unpinned(tmp_path):
    """Adding a role-playing shortcut silently re-routes existing queries
    (fewest hops wins). The numbers prove the flip; the warning is the
    tripwire that tells the author to pin a route."""
    runtime = Runtime.from_path(str(_write_geo_package(tmp_path, ship_city=True)))
    out = runtime.query(_query("measure.geo.revenue_usd", ["dimension.geo_region_name"]))

    assert _values_by_key(out["rows"], "dimension.geo_region_name") == REVENUE_BY_REGION_SHIP
    unpinned = [w for w in out["warnings"] if w["code"] == "PATH_ALTERNATES_UNPINNED"]
    assert len(unpinned) == 1
    assert unpinned[0]["details"]["target_entity"] == "entity.geo_region"
    assert unpinned[0]["details"]["alternate_path"] == CUSTOMER_ROUTE_TO_REGION


def test_unpinned_warning_suppressed_when_author_sets_path_preference(tmp_path):
    runtime = Runtime.from_path(
        str(_write_geo_package(tmp_path, ship_city=True, ship_city_preference=10))
    )
    out = runtime.query(_query("measure.geo.revenue_usd", ["dimension.geo_region_name"]))

    assert _values_by_key(out["rows"], "dimension.geo_region_name") == REVENUE_BY_REGION_SHIP
    assert not [w for w in out["warnings"] if w["code"] == "PATH_ALTERNATES_UNPINNED"]


@pytest.mark.parametrize("pref_key", ["preferred_paths", "relationship_path"])
def test_path_preferences_pin_the_declared_route(tmp_path, pref_key):
    """With the diamond present, a path_preferences pin must force the
    longer customer-home route — proven by the numbers, not the plan."""
    runtime = Runtime.from_path(
        str(
            _write_geo_package(
                tmp_path,
                ship_city=True,
                path_preferences=[
                    {
                        "source": "line_item",
                        "target": "region",
                        "path": CUSTOMER_ROUTE_TO_REGION,
                    }
                ],
                pref_key=pref_key,
            )
        )
    )
    out = runtime.query(_query("measure.geo.revenue_usd", ["dimension.geo_region_name"]))
    assert _values_by_key(out["rows"], "dimension.geo_region_name") == REVENUE_BY_REGION_HOME


def test_conflicting_routes_to_one_table_are_refused_not_silently_wrong(tmp_path):
    """Pin region to the customer-home route while city resolves via the
    ship-to shortcut: both need the `cities` table through different
    relationships. One table instance cannot serve both semantics."""
    runtime = Runtime.from_path(
        str(
            _write_geo_package(
                tmp_path,
                ship_city=True,
                path_preferences=[
                    {
                        "source": "line_item",
                        "target": "region",
                        "path": CUSTOMER_ROUTE_TO_REGION,
                    }
                ],
            )
        )
    )
    with pytest.raises(SemanticLayerError) as exc_info:
        runtime.query(
            _query(
                "measure.geo.revenue_usd",
                ["dimension.geo_region_name", "dimension.geo_city_name"],
            )
        )
    err = exc_info.value
    assert err.code == "PATH_JOIN_CONFLICT"
    assert err.details["table"] == "cities"
    assert {err.details["existing_relationship"], err.details["conflicting_relationship"]} == {
        "relationship.customers_city",
        "relationship.orders_ship_city",
    }


def test_consistent_pins_resolve_the_conflict(tmp_path):
    """Pinning BOTH region and city to the customer-home route makes the
    same query legal again — and the numbers follow the pinned route."""
    runtime = Runtime.from_path(
        str(
            _write_geo_package(
                tmp_path,
                ship_city=True,
                path_preferences=[
                    {
                        "source": "line_item",
                        "target": "region",
                        "path": CUSTOMER_ROUTE_TO_REGION,
                    },
                    {
                        "source": "line_item",
                        "target": "city",
                        "path": CUSTOMER_ROUTE_TO_REGION[:3],
                    },
                ],
            )
        )
    )
    out = runtime.query(
        _query(
            "measure.geo.revenue_usd",
            ["dimension.geo_region_name", "dimension.geo_city_name"],
        )
    )
    by_pair = {
        (row["dimension.geo_region_name"], row["dimension.geo_city_name"]): float(row["value"])
        for row in out["rows"]
    }
    assert by_pair == {
        ("East", "New York"): 45.0,
        ("Quebec", "Montreal"): 70.0,
        ("West", "Los Angeles"): 40.0,
        ("West", "San Francisco"): 60.0,
    }


def test_path_preferences_with_unknown_relationship_fail_at_load(tmp_path):
    with pytest.raises(SemanticLayerError) as exc_info:
        Runtime.from_path(
            str(
                _write_geo_package(
                    tmp_path,
                    path_preferences=[
                        {
                            "source": "line_item",
                            "target": "region",
                            "path": ["relationship.does_not_exist"],
                        }
                    ],
                )
            )
        )
    err = exc_info.value
    assert err.code == "INVALID_CONFIG"
    assert "does_not_exist" in str(err)


def test_path_preferences_that_do_not_reach_target_fail_at_load(tmp_path):
    with pytest.raises(SemanticLayerError) as exc_info:
        Runtime.from_path(
            str(
                _write_geo_package(
                    tmp_path,
                    path_preferences=[
                        {
                            "source": "line_item",
                            "target": "region",
                            # Stops at customer — never reaches region.
                            "path": CUSTOMER_ROUTE_TO_REGION[:2],
                        }
                    ],
                )
            )
        )
    err = exc_info.value
    assert err.code == "INVALID_CONFIG"
    assert "ends at" in str(err)
