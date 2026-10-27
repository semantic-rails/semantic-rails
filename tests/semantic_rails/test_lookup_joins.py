"""Lookups keep the rows they find no match for.

A many-to-one (or one-to-one) hop only adds attributes, so a row whose foreign key is NULL or
matches no row must keep its measure value when the query groups or filters by a dimension the
hop looks up: it groups under NULL, and grouped rows add up to the ungrouped total. A filter on
a looked-up dimension reads such a row as it reads a NULL value in the row itself: ``not`` keeps
it, and every other comparison drops it unless it asks for a missing value.
``test_lookups_keep_rows.py`` checks every query shape.

A few reads keep the inner join they always had (see the last section): a time role, a metric
filter's own query and its route to its entity, a conversion's converted events, and a
dimension a rollup holds. A metric filter's set is matched on its context entities, so a row
with none is not in it. A conversion's base events are its rate's denominator, so they keep
their rows: one with no match key counts and converts nothing.

Fixture: boardings of flight legs, with a crew roster keyed by (leg, person). Boarding 7 has
no person, boarding 8 a person with no record (P9), boarding 11 a leg with no record (L9),
boardings 12 and 13 no leg, and leg L3 an airport with no record (SFO). Daily stock levels of
regions: stock s3 has no region and s4 a region with no record (R9). The gold values come
from SQL written independently of the engine (scalar subqueries and NOT EXISTS, no outer
joins), run on the same database, and are also spelled out by hand.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.dialects import _WAREHOUSE_CONNECTORS
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from semantic_rails.schema import AggregateRelationConfig
from tests.semantic_rails.conftest import opened

SEED_SQL = """
CREATE TABLE airports (airport_code VARCHAR, city VARCHAR);
INSERT INTO airports VALUES ('JFK', 'New York'), ('ORD', 'Chicago');
CREATE TABLE legs (leg_id VARCHAR, airport_code VARCHAR, departure_date DATE);
INSERT INTO legs VALUES ('L1', 'JFK', DATE '2026-01-10'), ('L2', 'ORD', DATE '2026-01-20'),
  ('L3', 'SFO', DATE '2026-02-12');
CREATE TABLE people (person_id VARCHAR, full_name VARCHAR, is_employee BOOLEAN);
INSERT INTO people VALUES ('P1', 'Ana', FALSE), ('P2', 'Ben', FALSE), ('P3', 'Cy', TRUE),
  ('P4', 'Di', TRUE), ('P5', 'Ed', FALSE);
CREATE TABLE crew_roster (leg_id VARCHAR, person_id VARCHAR, crew_role VARCHAR);
INSERT INTO crew_roster VALUES ('L1', 'P3', 'operating'), ('L1', 'P4', 'deadhead'),
  ('L2', 'P4', 'operating'), ('L3', 'P3', 'operating');
CREATE TABLE boardings (boarding_id INTEGER, leg_id VARCHAR, person_id VARCHAR,
  boarded_at TIMESTAMP, fare DECIMAL(10, 2));
INSERT INTO boardings VALUES
  (1, 'L1', 'P1', TIMESTAMP '2026-01-10 08:00:00', 100),
  (2, 'L1', 'P2', TIMESTAMP '2026-01-10 08:05:00', 120),
  (3, 'L1', 'P3', TIMESTAMP '2026-01-10 07:30:00', 0),
  (4, 'L1', 'P4', TIMESTAMP '2026-01-10 07:40:00', 0),
  (5, 'L2', 'P1', TIMESTAMP '2026-01-20 09:00:00', 90),
  (6, 'L2', 'P4', TIMESTAMP '2026-01-20 08:30:00', 0),
  (7, 'L2', NULL, TIMESTAMP '2026-01-20 09:10:00', 50),
  (8, 'L3', 'P9', TIMESTAMP '2026-02-12 10:00:00', 80),
  (9, 'L3', 'P3', TIMESTAMP '2026-02-12 09:00:00', 60),
  (10, 'L3', 'P4', TIMESTAMP '2026-02-12 10:05:00', 70),
  (11, 'L9', 'P5', TIMESTAMP '2026-02-20 11:00:00', 40),
  (12, NULL, 'P5', TIMESTAMP '2026-03-01 09:00:00', 10),
  (13, NULL, 'P2', TIMESTAMP '2026-03-02 09:00:00', 10);
CREATE TABLE checkins (checkin_id INTEGER, person_id VARCHAR, leg_id VARCHAR,
  checked_in_at TIMESTAMP);
INSERT INTO checkins VALUES
  (1, 'P1', 'L1', TIMESTAMP '2026-01-10 06:00:00'),
  (2, NULL, 'L2', TIMESTAMP '2026-01-20 06:00:00'),
  (3, 'P2', NULL, TIMESTAMP '2026-01-01 06:00:00'),
  (4, 'P4', 'L7', TIMESTAMP '2026-01-01 06:00:00'),
  (5, 'P3', 'L3', TIMESTAMP '2026-02-12 06:00:00'),
  (6, 'P9', 'L3', TIMESTAMP '2026-02-12 06:00:00'),
  (7, 'P5', 'L9', TIMESTAMP '2026-02-20 06:00:00'),
  (8, 'P1', 'L2', TIMESTAMP '2026-01-20 06:00:00');
CREATE TABLE regions (region_id VARCHAR, region_name VARCHAR);
INSERT INTO regions VALUES ('R1', 'North'), ('R2', 'South');
CREATE TABLE stock_levels (stock_id VARCHAR, region_id VARCHAR, snapshot_date DATE, level INTEGER);
INSERT INTO stock_levels VALUES
  ('s1', 'R1', DATE '2026-01-01', 10), ('s1', 'R1', DATE '2026-01-02', 12),
  ('s2', 'R2', DATE '2026-01-01', 5), ('s2', 'R2', DATE '2026-01-02', 7),
  ('s3', NULL, DATE '2026-01-01', 3), ('s3', NULL, DATE '2026-01-02', 4),
  ('s4', 'R9', DATE '2026-01-02', 1);
CREATE TABLE meals (meal_id INTEGER, boarding_id INTEGER, served_at TIMESTAMP)
"""

PACKAGE = """
schema_version: 1
package:
  id: crew
  namespace: crew
  warehouse: duckdb
  default_db: data/crew.duckdb
  seed: {kind: sql_script, source: data/seed.sql}
defaults:
  dimension: {groupable: true, filterable: true}
"""

GRAPH = """
graph:
  entities:
    airport: {label: Airport, key: [airport_code], model: airports}
    leg: {label: Flight leg, key: [leg_id], model: legs}
    person: {label: Person, key: [person_id], model: people}
    crew_assignment: {label: Crew assignment, key: [leg_id, person_id], model: crew_roster}
    boarding: {label: Boarding, key: [boarding_id], model: boardings}
    checkin: {label: Check-in, key: [checkin_id], model: checkins}
    meal: {label: Meal, key: [meal_id], model: meals}
    region: {label: Region, key: [region_id], model: regions}
    stock_level: {label: Stock level, key: [stock_id, snapshot_date], model: stock_levels,
      allowed_as_root: true}
  # A boarding or a check-in also reaches a leg and a person through crew assignments, so
  # the package records what each question here means: the airport of the boarding's or
  # check-in's own leg, and a meal's boarding's leg, person and airport. (A boarding's own
  # leg and person are its direct keys, which need no row.)
  path_preferences:
    - source_entity: boarding
      target_entity: airport
      relationship_path: [boardings_leg, legs_airport]
    - source_entity: checkin
      target_entity: airport
      relationship_path: [checkins_leg, legs_airport]
    - source_entity: meal
      target_entity: leg
      relationship_path: [meals_boarding, boardings_leg]
    - source_entity: meal
      target_entity: person
      relationship_path: [meals_boarding, boardings_person]
    - source_entity: meal
      target_entity: airport
      relationship_path: [meals_boarding, boardings_leg, legs_airport]
"""

# Each model's id and relation are its name.
MODELS = {
    "airports": """
        entities: {airport: {}}
        dimensions: {city: {label: City, kind: categorical}}
        """,
    "legs": """
        entities: {leg: {}, airport: {}}
        times:
          departure_date: {label: Departure date, column: departure_date, kind: date,
            class: event_time, supported_grains: [day, month]}
        """,
    "people": """
        entities: {person: {}}
        dimensions: {is_employee: {label: Is employee, kind: boolean}}
        """,
    "crew_roster": """
        entities: {crew_assignment: {}, leg: {}, person: {}}
        dimensions: {crew_role: {label: Crew role, kind: categorical}}
        """,
    "boardings": """
        entities: {boarding: {}, leg: {}, person: {}, crew_assignment: {}}
        times:
          boarded_at: {label: Boarded at, column: boarded_at, kind: timestamp,
            class: event_time, supported_grains: [day, month], default: true}
        measures:
          boarding_count: {label: Boardings, kind: entity_count, entity_key: boarding_id,
            accumulation: {kind: event}}
          fare: {label: Fare, kind: aggregate, expr: fare, default_agg: sum,
            accumulation: {kind: flow}}
          boarding_population: {label: Boarding population, kind: entity_count,
            entity_key: boarding_id, accumulation: {kind: population}}
          departing_boardings: {label: Boardings by departure, kind: entity_count,
            entity_key: boarding_id, accumulation: {kind: event},
            times: [temporal_role.crew_leg_departure_date]}
        """,
    "checkins": """
        entities: {checkin: {}, person: {}, leg: {}}
        times:
          checked_in_at: {label: Checked in at, column: checked_in_at, kind: timestamp,
            class: event_time, supported_grains: [day, month], default: true}
        measures:
          checkin_count: {label: Check-ins, kind: entity_count, entity_key: checkin_id,
            accumulation: {kind: event}}
        """,
    "meals": """
        entities: {meal: {}, boarding: {}}
        times:
          served_at: {label: Served at, column: served_at, kind: timestamp,
            class: event_time, supported_grains: [day, month], default: true}
        measures:
          meal_count: {label: Meals, kind: entity_count, entity_key: meal_id,
            accumulation: {kind: event}}
        """,
    "regions": """
        entities: {region: {}}
        dimensions: {region_name: {label: Region name, kind: categorical}}
        """,
    "stock_levels": """
        entities: {stock_level: {}, region: {}}
        times:
          snapshot_date: {label: Snapshot date, column: snapshot_date, kind: date,
            class: as_of_time, supported_grains: [day, month], default: true}
        measures:
          level: {label: Level, kind: aggregate, expr: level, default_agg: sum,
            accumulation: {kind: stock, snapshot: end_of_period}}
        """,
}

ROLE = "dimension.crew_crew_assignment_crew_role"
EMPLOYEE = "dimension.crew_person_is_employee"
CITY = "dimension.crew_airport_city"
LEG = "dimension.crew_leg_id"
BOARDING = "dimension.crew_boarding_id"
PERIOD = "period"  # the time bucket's column

# Scalar subqueries read NULL where the lookup finds no row, independently of any join.
SQL_ROLE = (
    "(SELECT r.crew_role FROM crew_roster AS r"
    " WHERE r.leg_id = b.leg_id AND r.person_id = b.person_id)"
)
SQL_EMPLOYEE = "(SELECT p.is_employee FROM people AS p WHERE p.person_id = b.person_id)"
SQL_CITY = (
    "(SELECT a.city FROM legs AS l, airports AS a"
    " WHERE l.leg_id = b.leg_id AND a.airport_code = l.airport_code)"
)


def _write_package(root: Path, extra_seed: str = "") -> Path:
    pkg = root / "crew"
    (pkg / "models").mkdir(parents=True)
    (pkg / "data").mkdir()
    (pkg / "data" / "seed.sql").write_text(SEED_SQL + extra_seed)
    (pkg / "package.yml").write_text(PACKAGE)
    (pkg / "graph.yml").write_text(GRAPH)
    for name, body in MODELS.items():
        model = {"id": name, "relation": name, **yaml.safe_load(body)}
        (pkg / "models" / f"{name}.yml").write_text(yaml.safe_dump({"model": model}))
    return pkg


def _runtime(root: Path, extra_seed: str = ""):
    runtime = Runtime.from_path(str(_write_package(root, extra_seed)))
    yield opened(runtime)
    runtime.close()


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory):
    yield from _runtime(tmp_path_factory.mktemp("lookups"))


# A leg whose airport has a record but no city, with check-ins 9 (P6) and 10 (P7): boarding 14
# is on the same leg as check-in 9, and boarding 15 on Chicago's leg L2.
NULL_CITY_SEED = """;
INSERT INTO airports VALUES ('DEN', NULL);
INSERT INTO legs VALUES ('L4', 'DEN', DATE '2026-03-05');
INSERT INTO people VALUES ('P6', 'Flo', FALSE), ('P7', 'Gus', FALSE);
INSERT INTO checkins VALUES
  (9, 'P6', 'L4', TIMESTAMP '2026-03-05 06:00:00'),
  (10, 'P7', 'L4', TIMESTAMP '2026-03-05 06:00:00');
INSERT INTO boardings VALUES
  (14, 'L4', 'P6', TIMESTAMP '2026-03-05 07:00:00', 30),
  (15, 'L2', 'P7', TIMESTAMP '2026-03-05 07:00:00', 30)
"""


@pytest.fixture(scope="module")
def null_city_runtime(tmp_path_factory: pytest.TempPathFactory):
    yield from _runtime(tmp_path_factory.mktemp("null_city"), NULL_CITY_SEED)


# Person P8 has two boardings with no leg and one on a leg with no record (L8). A meal on the
# last is two hops from the leg, so it reads NULL for its leg, yet the set of people with two
# boardings holds P8 under NULL (two boardings) and under L8 (one). P10 has two boardings on
# L1, with a meal on one.
ORPHAN_LEG_SEED = """;
INSERT INTO people VALUES ('P8', 'Hal', FALSE), ('P10', 'Ivy', FALSE);
INSERT INTO boardings VALUES
  (20, NULL, 'P8', TIMESTAMP '2026-01-05 08:00:00', 10),
  (21, NULL, 'P8', TIMESTAMP '2026-01-05 08:10:00', 10),
  (22, 'L8', 'P8', TIMESTAMP '2026-01-05 08:20:00', 10),
  (30, 'L1', 'P10', TIMESTAMP '2026-01-06 08:00:00', 10),
  (31, 'L1', 'P10', TIMESTAMP '2026-01-06 08:10:00', 10);
INSERT INTO meals VALUES (1, 22, TIMESTAMP '2026-01-05 09:00:00'),
  (2, 30, TIMESTAMP '2026-01-06 09:00:00')
"""


@pytest.fixture(scope="module")
def orphan_leg_runtime(tmp_path_factory: pytest.TempPathFactory):
    yield from _runtime(tmp_path_factory.mktemp("orphan_leg"), ORPHAN_LEG_SEED)


@pytest.fixture(scope="module")
def gold():
    connections = {seed: duckdb.connect() for seed in ("", NULL_CITY_SEED)}
    for seed, connection in connections.items():
        connection.execute(SEED_SQL + seed)

    def run(sql: str, extra_seed: str = "") -> dict[Any, float]:
        rows = connections[extra_seed].execute(sql).fetchall()
        return {row[0]: float(row[1]) for row in rows}

    yield run
    for connection in connections.values():
        connection.close()


def _ask(runtime: Runtime, measure: str, *, group_by: str = "", where=()) -> dict[Any, float]:
    query: dict[str, Any] = {
        "select": [{"as": "value", "expression": f"measure.crew.{measure}"}],
        "where": list(where),
    }
    if group_by:
        query["group_by"] = [group_by]
    rows = runtime.query(query)["rows"]
    return {row.get(group_by) if group_by else None: float(row["value"]) for row in rows}


def _conversion_query(properties: tuple[str, ...] = (), **extra: Any) -> dict[str, Any]:
    expression = {
        **({"constant_properties": list(properties)} if properties else {}),
        "kind": "conversion",
        "entity": "entity.crew_person",
        "window": {"unit": "day", "value": 1},
        "matching_mode": "first_converted_after_base",
        "base": {"kind": "aggregate", "measure": "measure.crew.checkin_count"},
        "converted": {"kind": "aggregate", "measure": "measure.crew.boarding_count"},
    }
    return {"version": 1, "select": [{"as": "rate", "expression": expression}], **extra}


def _conversion_rate(
    runtime: Runtime, properties: tuple[str, ...] = (), **extra: Any
) -> list[dict[str, Any]]:
    return runtime.query(_conversion_query(properties, **extra))["rows"]


@pytest.mark.parametrize(
    ("dimension", "expected"),
    [
        pytest.param(EMPLOYEE, {False: 6, True: 5, None: 2}, id="person"),
        pytest.param(ROLE, {None: 9, "operating": 3, "deadhead": 1}, id="composite-key-roster"),
        # Two hops: a leg with no record (L9), no leg (12, 13) and an airport with no record.
        pytest.param(CITY, {"New York": 4, "Chicago": 3, None: 6}, id="two-hops"),
    ],
)
def test_grouped_rows_add_up_to_the_total(runtime, gold, dimension, expected):
    grouped = _ask(runtime, "boarding_count", group_by=dimension)

    assert grouped == expected
    assert sum(grouped.values()) == _ask(runtime, "boarding_count")[None] == 13
    sql = {EMPLOYEE: SQL_EMPLOYEE, ROLE: SQL_ROLE, CITY: SQL_CITY}[dimension]
    assert grouped == gold(f"SELECT {sql}, COUNT(*) FROM boardings AS b GROUP BY 1")


def test_excluding_crew_through_the_roster_lookup(runtime, gold):
    """Passengers per leg, excluding anyone rostered as crew on that leg (an anti-join)."""
    not_crew = [{"field": ROLE, "op": "missing", "value": True}]

    per_leg = _ask(runtime, "boarding_count", group_by=LEG, where=not_crew)

    assert per_leg == {"L1": 2, "L2": 2, "L3": 2, "L9": 1, None: 2}
    assert per_leg == gold(
        "SELECT b.leg_id, COUNT(*) FROM boardings AS b WHERE NOT EXISTS (SELECT 1 FROM"
        " crew_roster AS r WHERE r.leg_id = b.leg_id AND r.person_id = b.person_id) GROUP BY 1"
    )
    assert _ask(runtime, "boarding_count", where=not_crew) == {None: 9}


def test_measures_folded_into_one_leaf_keep_the_rows_too(runtime, gold):
    """Two measures of one entity share a leaf, which reads the lookup with its own code path:
    it must keep the rows with no match (boardings 7 and 8: a NULL person and one with no
    record, under NULL) as a single measure does."""
    query = {
        "select": [
            {"as": "boardings", "expression": "measure.crew.boarding_count"},
            "measure.crew.fare",
        ],
        "group_by": [EMPLOYEE],
    }

    rows = runtime.query(query)["rows"]

    assert {row[EMPLOYEE]: (row["boardings"], float(row["fare"])) for row in rows} == {
        False: (6, 370),
        True: (5, 130),
        None: (2, 130),
    }
    assert sum(row["boardings"] for row in rows) == _ask(runtime, "boarding_count")[None] == 13
    assert {row[EMPLOYEE]: float(row["fare"]) for row in rows} == gold(
        f"SELECT {SQL_EMPLOYEE}, SUM(b.fare) FROM boardings AS b GROUP BY 1"
    )


def test_a_semi_additive_measure_keeps_the_rows_with_no_match(runtime):
    """A stock measure takes each stock's last snapshot of the month within a group, so a NULL
    group has snapshots of its own to pick from: stock s3 (no region) and s4 (a region with no
    record) are read at their last day and group under NULL."""
    region = "dimension.crew_region_region_name"
    query = {
        "select": [{"as": "value", "expression": "measure.crew.level"}],
        "group_by": [region],
        "time": {"temporal_role": "temporal_role.crew_stock_level_snapshot_date", "grain": "month"},
    }

    by_region = {row[region]: row["value"] for row in runtime.query(query)["rows"]}

    assert by_region == {"North": 12, "South": 7, None: 5}  # s3 at 4, s4 at 1
    assert sum(by_region.values()) == 24  # each stock at its last snapshot


@pytest.mark.parametrize(
    ("op", "value", "sql", "expected"),
    [
        pytest.param("missing", True, "{role} IS NULL", 9, id="is-null"),
        pytest.param("missing", False, "{role} IS NOT NULL", 4, id="is-not-null"),
        pytest.param("is", "operating", "{role} = 'operating'", 3, id="equals"),
        # A row with no roster match has no role: like a NULL role, it is "not operating"
        # (boardings 1, 2, 5, 7, 8, 10-13, and deadhead 4).
        pytest.param(
            "not", "operating", "{role} IS DISTINCT FROM 'operating'", 10, id="not-equals"
        ),
        # Only the nine boardings with no roster match.
        pytest.param(
            "not",
            ["operating", "deadhead"],
            "{role} IS NULL OR {role} NOT IN ('operating', 'deadhead')",
            9,
            id="not-in",
        ),
    ],
)
def test_filters_on_a_looked_up_column_follow_the_null_rules(
    runtime, gold, op, value, sql, expected
):
    where = [{"field": ROLE, "op": op, "value": value}]

    got = _ask(runtime, "boarding_count", where=where)[None]

    assert got == expected
    reference = gold(f"SELECT NULL, COUNT(*) FROM boardings AS b WHERE {sql.format(role=SQL_ROLE)}")
    assert got == reference[None]


# Check-ins that lead to a boarding by the same person within a day, matched on the events' own
# person_id. Every check-in counts in the rate. Check-in 2 (no person) matches nothing; check-in
# 6 and boarding 8 share P9, whom the people table doesn't hold (a late-arriving person), and
# pair as they do in plain SQL. 1, 5, 6, 7 and 8 convert; 2, 3 and 4 don't.
CONVERSION_SQL = """
SELECT {group}, AVG(CASE WHEN EXISTS (
  SELECT 1 FROM boardings AS b WHERE b.person_id = c.person_id
  AND b.boarded_at >= c.checked_in_at AND b.boarded_at < c.checked_in_at + INTERVAL 1 DAY
  {same_city}
) THEN 1.0 ELSE 0.0 END)
FROM checkins AS c WHERE TRUE {where}
GROUP BY 1
"""
SQL_CHECKIN_EMPLOYEE = "(SELECT p.is_employee FROM people AS p WHERE p.person_id = c.person_id)"
# The airport city, read through the leg: NULL for a check-in or boarding whose leg (or the
# leg's airport) has no record.
SQL_CHECKIN_CITY = (
    "(SELECT a.city FROM legs AS l, airports AS a"
    " WHERE l.leg_id = c.leg_id AND a.airport_code = l.airport_code)"
)


def _conversion_sql(*, group: str = "NULL", same_city: str = "", where: str = "") -> str:
    return CONVERSION_SQL.format(group=group, same_city=same_city, where=where)


def test_conversion_pairs_on_the_events_own_key(runtime, gold):
    """Events pair on their own person_id. An event with no person pairs with nothing, not with
    another event without one (check-in 2 with boarding 7). Check-in 6 and boarding 8 share P9,
    whom the people table doesn't hold, and pair: a late-arriving person still converts. Every
    check-in counts in the rate."""
    (row,) = _conversion_rate(runtime)
    assert row["rate"] == pytest.approx(5 / 8)
    assert row["rate"] == pytest.approx(gold(_conversion_sql())[None])

    by_employee = _conversion_rate(runtime, group_by=[EMPLOYEE])
    rates = {row[EMPLOYEE]: row["rate"] for row in by_employee}
    # 1, 7, 8 of 1, 3, 7, 8; 5 of 4, 5; 6 of 2, 6 (no person record: the NULL group).
    assert rates == {
        False: pytest.approx(0.75),
        True: pytest.approx(0.5),
        None: pytest.approx(0.5),
    }
    assert rates == pytest.approx(gold(_conversion_sql(group=SQL_CHECKIN_EMPLOYEE)))


def test_conversion_pairs_on_a_matched_lookup_whose_property_is_null(null_city_runtime, gold):
    """Events also pair on constant properties, here the airport city read through the leg.
    Check-in 7 and boarding 11 (leg L9) and check-in 5 and boarding 9 (an airport with no
    record) have no city, so they must not pair as having "the same city": those check-ins
    count and don't convert. Where the lookup matched but the property is NULL, as it is on the
    event's own entity, check-in 9 pairs with boarding 14 (both at an airport with no city) and
    not with boarding 15 (Chicago)."""
    # A leg whose airport row exists: its city, NULL or not, is a match.
    matched = "SELECT l.leg_id FROM legs AS l JOIN airports AS a ON a.airport_code = l.airport_code"
    reference = gold(
        _conversion_sql(
            same_city=f"AND c.leg_id IN ({matched}) AND b.leg_id IN ({matched})"
            f" AND {SQL_CITY} IS NOT DISTINCT FROM {SQL_CHECKIN_CITY}",
        ),
        NULL_CITY_SEED,
    )

    (row,) = _conversion_rate(null_city_runtime, (CITY,))

    # Check-ins 1, 8 and 9 convert; 2-7 and 10 don't, and every one of the 10 counts.
    assert row["rate"] == pytest.approx(0.3)
    assert reference[None] == pytest.approx(row["rate"])


def test_conversion_predicate_does_not_qualify_rows_without_its_entity(runtime, gold):
    """Legs with two boardings or more: L1, L2 and L3. Boardings 12 and 13 (no leg) must not
    qualify the check-ins without a leg (3) or with a leg that has no record (4)."""
    busy_legs = {
        "entity": "entity.crew_leg",
        "having": [{"field": "measure.crew.boarding_count", "op": "gte", "value": 2}],
        "over": "all_time",
    }

    (row,) = _conversion_rate(runtime, where=[busy_legs])

    busy = "SELECT leg_id FROM boardings GROUP BY leg_id HAVING COUNT(*) >= 2"
    reference = gold(_conversion_sql(where=f"AND c.leg_id IN ({busy})"))[None]
    # Check-ins 1, 2, 5, 6 and 8 are on busy legs; 1, 5, 6 and 8 convert, and 2 has no person
    # to match. 3 and 4 aren't in the rate at all.
    assert row["rate"] == pytest.approx(4 / 5)
    assert reference == pytest.approx(row["rate"])


# One warehouse per dialect family: DuckDB and DuckLake render as MotherDuck does.
FAMILIES = ["motherduck", "postgres", "snowflake", "bigquery", "databricks", "athena", "clickhouse"]


def test_the_dialect_decides_the_lookup_join_type(runtime):
    """ClickHouse reads '' or 0 (not NULL) from an unmatched outer-join column unless it is
    Nullable, so its lookups stay inner joins and drop the rows they find no match for."""
    assert {*FAMILIES, "duckdb", "ducklake"} == set(_WAREHOUSE_CONNECTORS)
    query = {
        "select": [{"as": "value", "expression": {"measure": "measure.crew.boarding_count"}}],
        "group_by": [CITY],
        "where": [{"field": ROLE, "op": "missing", "value": True}],
        "observation_scope": "query",  # the leaf's joins alone, without a dataset probe's
    }
    for warehouse in FAMILIES:
        package = dataclasses.replace(runtime.config.package, warehouse=warehouse)
        config = dataclasses.replace(runtime.config, package=package)

        sql = " ".join(compile_query(config, Registry(config), query)["sql"].split())

        # The roster, the leg and the airport: three lookup hops.
        join_type = "INNER" if warehouse == "clickhouse" else "LEFT"
        assert re.findall(r"\b(\w+) JOIN\b", sql) == [join_type] * 3, warehouse


BOARDED_MONTHLY = {"temporal_role": "temporal_role.crew_boarding_boarded_at", "grain": "month"}
DEPARTED_MONTHLY = {"temporal_role": "temporal_role.crew_leg_departure_date", "grain": "month"}
LOOKUP_TABLES = ("airports", "legs", "people", "crew_roster")


def _on_busy(measure: str, *minimums: int, entity: str = "leg") -> dict[str, Any]:
    """The ``measure``'s distinct count over each ``entity`` with each of ``minimums`` or more."""
    measure = f"measure.crew.{measure}"
    predicates = [
        {"measure": measure, "entity": f"entity.crew_{entity}", "op": ">=", "value": minimum}
        for minimum in minimums
    ]
    return {
        "kind": "scoped_aggregate",
        "measure": measure,
        "aggregation": "count_distinct",
        "predicates": predicates,
    }


def _monthly_query(expression: Any, *, group_by=(), time=None) -> dict[str, Any]:
    return {
        "version": 1,
        "select": [{"as": "value", "expression": expression}],
        "time": time or BOARDED_MONTHLY,
        **({"group_by": list(group_by)} if group_by else {}),
    }


def _metric_filter(entity: str, scope_mode: str, op: str, boardings: int) -> dict[str, Any]:
    """The engine's metric filter on the ``entity``'s boarding count."""
    return {
        "expression": {
            "kind": "metric_predicate",
            "entity": f"entity.crew_{entity}",
            "scope_mode": scope_mode,
            "input": {"measure": "measure.crew.boarding_count"},
            "op": op,
            "value": boardings,
        },
        "op": "=",
        "value": True,
    }


def _of_people_with_two_boardings(measure: str, group_by: str) -> dict[str, Any]:
    return {
        "select": [{"as": "value", "expression": {"measure": f"measure.crew.{measure}"}}],
        "group_by": [group_by],
        "metric_filters": [_metric_filter("person", "contextual", "=", 2)],
    }


def _lookup_left_joins(config, query: dict[str, Any]) -> list[str]:
    sql = " ".join(compile_query(config, Registry(config), query)["sql"].split())
    return re.findall(rf"LEFT JOIN ({'|'.join(LOOKUP_TABLES)})\b", sql)


@pytest.mark.parametrize(
    ("group_by", "expected"),
    [
        # The person hop, which only the grouping reads, keeps its rows: boardings 7 and 8.
        (EMPLOYEE, {(False, 1): 3, (True, 1): 3, (None, 1): 1, (True, 2): 2, (None, 2): 1}),
        # The airport city is read through the leg, which the time role reads too: the leg hop
        # is inner, the airport hop keeps its rows (boardings 8-10, whose airport has no record).
        (CITY, {("New York", 1): 4, ("Chicago", 1): 3, (None, 2): 3}),
    ],
    ids=["own-hop", "hop-shared-with-the-time-role"],
)
def test_a_time_role_read_through_a_lookup_keeps_the_inner_join(runtime, group_by, expected):
    """Departing boardings by month of the leg's departure. The leg is what the time role reads,
    so a boarding with no leg record (11, 12, 13) has no month: it stays out, as before, and no
    NULL time bucket appears."""
    query = _monthly_query(
        "measure.crew.departing_boardings", group_by=[group_by], time=DEPARTED_MONTHLY
    )

    rows = runtime.query(query)["rows"]

    assert {
        (row[group_by], datetime.fromisoformat(row[PERIOD]).month): row["value"] for row in rows
    } == expected


def test_a_dimension_only_query_keeps_the_time_role_inner(runtime, gold):
    """Boardings by the month their leg departed, dimensions alone. A boarding with no leg
    record (11, 12, 13) has no month: it stays out, as the time role does in every leaf, and
    never shows a NULL month. The airport hop, which only the grouping reads, keeps its rows
    (boardings 8-10, whose airport has no record, under NULL)."""
    rows = runtime.query({"group_by": [BOARDING, CITY], "time": DEPARTED_MONTHLY})["rows"]

    months = {row[BOARDING]: datetime.fromisoformat(row[PERIOD]).month for row in rows}
    assert months == gold(
        "SELECT b.boarding_id, (SELECT MONTH(l.departure_date) FROM legs AS l"
        " WHERE l.leg_id = b.leg_id) FROM boardings AS b"
        " WHERE EXISTS (SELECT 1 FROM legs AS l WHERE l.leg_id = b.leg_id)"
    )
    assert set(months) == set(range(1, 11))
    assert {row[BOARDING]: row[CITY] for row in rows} == {
        **dict.fromkeys(range(1, 5), "New York"),
        **dict.fromkeys(range(5, 8), "Chicago"),
        **dict.fromkeys(range(8, 11), None),
    }


def test_a_metric_filter_matches_its_set_on_its_context_entity(runtime, gold):
    """Boardings whose leg has any boarding that month, grouped by the boarder's kind: the
    person is a context entity of the filter. The filter's set is matched on the person, so a
    boarding whose person lookup found nothing (7 and 8) has no set to be in: it stays out, as
    before, although the person hop joins LEFT like every grouping's."""
    query = _monthly_query(_on_busy("boarding_count", 1), group_by=[EMPLOYEE])

    rows = runtime.query_internal(query)["rows"]

    by_employee: dict[Any, int] = {}
    for row in rows:
        by_employee[row[EMPLOYEE]] = by_employee.get(row[EMPLOYEE], 0) + row["value"]
    assert by_employee == {False: 4, True: 5}
    assert by_employee == gold(
        f"SELECT {SQL_EMPLOYEE}, COUNT(*) FROM boardings AS b"
        f" WHERE b.leg_id IS NOT NULL AND {SQL_EMPLOYEE} IS NOT NULL GROUP BY 1"
    )


def test_an_entity_set_ratio_keeps_the_inner_join(runtime):
    """Of each month's boardings on a leg, the share on a leg with two boardings or more that
    month. Boardings 12 and 13 have no leg, so no set qualifies them: March's share is 0.0, as
    the inner joins have always answered, and never the 1.0 a NULL key in the set would give."""
    population = "boarding_population"  # a population count takes the anchored entity-set path
    ratio = {
        "kind": "ratio",
        "numerator": _on_busy(population, 1, 2),
        "denominator": _on_busy(population, 1),
    }

    rows = runtime.query_internal(_monthly_query(ratio))["rows"]

    months = {datetime.fromisoformat(row[PERIOD]).month: row["value"] for row in rows}
    # January: legs L1 (4) and L2 (3), all qualify. February: L3 (3) qualifies, L9 (1) doesn't.
    assert months == {1: pytest.approx(7 / 7), 2: pytest.approx(3 / 4), 3: 0.0}


def test_a_contextual_filter_read_through_a_lookup_does_not_pair_with_a_null_group(
    orphan_leg_runtime,
):
    """Meals per leg, for people with exactly two boardings on that leg. The meal is two hops
    from the leg, so its leg is read through the leg table. Meal 1 is on boarding 22 (leg L8, no
    record): it reads NULL there, and must not pair with P8's two boardings with no leg, which
    the filter's set holds under NULL. Only P10's meal on L1 qualifies."""
    query = _of_people_with_two_boardings("meal_count", LEG)

    rows = orphan_leg_runtime.query_internal(query)["rows"]

    assert {row[LEG]: row["value"] for row in rows} == {"L1": 1}


def test_a_dimension_a_rollup_holds_keeps_the_inner_join(runtime):
    """The base tables answer as a rollup that pre-joined the dimension does, so routing never
    changes an answer: a rollup of the boardings that holds the airport city keeps every
    lookup of that city inner, however the query reaches it."""
    rollup = AggregateRelationConfig(
        id="aggregate_relation.city",
        relation="boardings_by_city",
        source_entity="entity.crew_boarding",
        dimensions=[CITY],
    )
    query = {
        "select": [{"as": "value", "expression": {"measure": "measure.crew.boarding_count"}}],
        "group_by": [CITY, EMPLOYEE],
    }

    with_rollup = dataclasses.replace(runtime.config, aggregate_relations=[rollup])

    assert _lookup_left_joins(runtime.config, query) == ["legs", "airports", "people"]
    assert _lookup_left_joins(with_rollup, query) == ["people"]


# The conversions and the entity-set ratio above check their rows; these reads check the join.
@pytest.mark.parametrize(
    ("query", "left_joins"),
    [
        # A conversion matches on the events' own person_id, so neither side joins people; the
        # leg its metric filter is matched on stays inner.
        (
            _conversion_query(metric_filters=[_metric_filter("leg", "entity_only", ">=", 2)]),
            [],
        ),
        (_monthly_query(_on_busy("boarding_count", 1)), []),
        # The set's own query groups by the airport, read through the leg: a nested query.
        (_monthly_query(_on_busy("boarding_count", 1, entity="airport")), []),
        # A metric filter leaves the query's own lookups LEFT. Its route to the person walks
        # the boarding hop, which stays inner; the leg is its context entity, matched in its
        # set (see the orphan-leg test above).
        (_of_people_with_two_boardings("meal_count", LEG), ["legs"]),
        (_of_people_with_two_boardings("boarding_count", EMPLOYEE), ["people"]),
    ],
    ids=[
        "conversion-predicate-set",
        "qualified-set",
        "nested-query-of-a-qualified-set",
        "metric-filter-context",
        "metric-filter-entity-grouped",
    ],
)
def test_every_other_read_of_a_lookup_keeps_the_inner_join(runtime, query, left_joins):
    assert _lookup_left_joins(runtime.config, query) == left_joins


# A conversion authored with a filter on its base events: check-ins of passengers, the people
# who aren't employees. "not true" keeps a check-in with no person record (its is_employee reads
# NULL), as it keeps a NULL in the row itself.
PASSENGERS = {"all": [{"field": EMPLOYEE, "op": "not", "value": True}]}
PASSENGER_CONVERSION = "metric.crew.passenger_conversion"


@pytest.fixture(scope="module")
def passenger_runtime(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("passengers")
    package = _write_package(root)
    (package / "metrics").mkdir()
    expression = _conversion_query()["select"][0]["expression"]
    expression["base"] = {**expression["base"], "filter": PASSENGERS}
    metric = {
        "label": "Passenger conversion",
        "kind": "conversion",
        "value_type": "percent",
        "expression": expression,
    }
    (package / "metrics" / "conversion.yml").write_text(
        yaml.safe_dump({"metrics": {"passenger_conversion": metric}})
    )
    runtime = Runtime.from_path(str(package))
    yield opened(runtime)
    runtime.close()


SQL_CHECKIN_LEG = "(SELECT l.leg_id FROM legs AS l WHERE l.leg_id = c.leg_id)"
PASSENGER_RATE_BY_LEG_SQL = f"""
SELECT {SQL_CHECKIN_LEG}, AVG(CASE WHEN EXISTS (
  SELECT 1 FROM boardings AS b WHERE b.person_id = c.person_id
  AND b.boarded_at >= c.checked_in_at AND b.boarded_at < c.checked_in_at + INTERVAL 1 DAY
) THEN 1.0 ELSE 0.0 END)
FROM checkins AS c WHERE {SQL_CHECKIN_EMPLOYEE} IS DISTINCT FROM TRUE
GROUP BY 1
"""


def test_a_conversion_filtered_on_reads_the_events_its_base_filter_keeps(passenger_runtime, gold):
    """The rate shown per leg and the rate a leg filter reads are one rate. Check-in 2 (no
    person) and 6 (P9, no person record) pass "is employee not true"; 6 converts on its own key
    (late-arriving P9) and 2 can't. So L2 is 1/2 and L3 is 1/1, and legs with a rate of at least
    0.6 are L1 and L3."""
    shown = passenger_runtime.query({"select": [PASSENGER_CONVERSION], "group_by": [LEG]})["rows"]
    rates = {row[LEG]: row["passenger_conversion"] for row in shown}
    reference = gold(PASSENGER_RATE_BY_LEG_SQL)
    assert rates == {leg: pytest.approx(rate) for leg, rate in reference.items()}
    assert rates["L2"] == pytest.approx(0.5) and rates["L3"] == pytest.approx(1.0)

    qualifying = {leg for leg, rate in reference.items() if leg is not None and rate >= 0.6}
    assert qualifying == {"L1", "L3"}
    fares = passenger_runtime.query(
        {
            "select": ["measure.crew.fare"],
            "group_by": [LEG],
            "where": [
                {
                    "entity": "entity.crew_leg",
                    "having": [{"field": PASSENGER_CONVERSION, "op": "gte", "value": 0.6}],
                    "over": "all_time",
                }
            ],
        }
    )["rows"]
    fare_sql = "SELECT b.leg_id, SUM(b.fare) FROM boardings AS b GROUP BY 1"
    expected = {leg: fare for leg, fare in gold(fare_sql).items() if leg in qualifying}
    assert {row[LEG]: float(row["fare"]) for row in fares} == {
        leg: pytest.approx(float(fare)) for leg, fare in expected.items()
    }
    # The same filter without a grouping: the qualifying legs' total fare (L1 220 + L3 210).
    (total,) = passenger_runtime.query(
        {
            "select": ["measure.crew.fare"],
            "where": [
                {
                    "entity": "entity.crew_leg",
                    "having": [{"field": PASSENGER_CONVERSION, "op": "gte", "value": 0.6}],
                    "over": "all_time",
                }
            ],
        }
    )["rows"]
    assert float(total["fare"]) == pytest.approx(sum(float(fare) for fare in expected.values()))


@pytest.mark.parametrize(
    ("condition", "where_sql"),
    [
        # The matched person's own attribute, one hop from the check-in.
        ({"field": EMPLOYEE, "op": "is", "value": False}, f"AND {SQL_CHECKIN_EMPLOYEE} = FALSE"),
        # Another looked-up column, two hops away (leg, then airport).
        ({"field": CITY, "op": "is", "value": "New York"}, f"AND {SQL_CHECKIN_CITY} = 'New York'"),
    ],
    ids=["person-attribute", "two-hop-city"],
)
def test_a_where_on_a_looked_up_base_column_answers(runtime, gold, condition, where_sql):
    (row,) = _conversion_rate(runtime, where=[condition])
    assert row["rate"] == pytest.approx(gold(_conversion_sql(where=where_sql))[None])
