"""Lookups keep the rows they find no match for.

A many-to-one (or one-to-one) hop only adds attributes, so a row whose foreign key is NULL or
matches no row must keep its measure value: it groups under NULL for the looked-up dimensions,
and grouped rows add up to the ungrouped total. A filter on a looked-up dimension still drops
such rows unless it asks for NULL, as it does for a NULL value in the row itself.

Fixture: boardings of flight legs, with a crew roster keyed by (leg, person). Boarding 7 has
no person, boarding 8 a person with no record (P9), boarding 11 a leg with no record (L9),
boardings 12 and 13 no leg, and leg L3 an airport with no record (SFO). The gold values come
from SQL written independently of the engine (scalar subqueries and NOT EXISTS, no outer
joins), run on the same database, and are also spelled out by hand.
"""

from __future__ import annotations

import dataclasses
import re
import textwrap
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.dialects import _WAREHOUSE_CONNECTORS
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime

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
"""

MODELS = {
    "airports": """
        model:
          id: airports
          relation: airports
          entities: {airport: {}}
          dimensions:
            city: {label: City, kind: categorical}
        """,
    "legs": """
        model:
          id: legs
          relation: legs
          entities: {leg: {}, airport: {}}
          times:
            departure_date: {label: Departure date, column: departure_date, kind: date,
              class: event_time, supported_grains: [day, month]}
        """,
    "people": """
        model:
          id: people
          relation: people
          entities: {person: {}}
          dimensions:
            full_name: {label: Name, kind: categorical}
            is_employee: {label: Is employee, kind: boolean}
        """,
    "crew_roster": """
        model:
          id: crew_roster
          relation: crew_roster
          entities: {crew_assignment: {}, leg: {}, person: {}}
          dimensions:
            crew_role: {label: Crew role, kind: categorical}
        """,
    "boardings": """
        model:
          id: boardings
          relation: boardings
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
        """,
    "checkins": """
        model:
          id: checkins
          relation: checkins
          entities: {checkin: {}, person: {}, leg: {}}
          times:
            checked_in_at: {label: Checked in at, column: checked_in_at, kind: timestamp,
              class: event_time, supported_grains: [day, month], default: true}
          measures:
            checkin_count: {label: Check-ins, kind: entity_count, entity_key: checkin_id,
              accumulation: {kind: event}}
        """,
    "meals": """
        model:
          id: meals
          relation: meals
          entities: {meal: {}, boarding: {}}
          times:
            served_at: {label: Served at, column: served_at, kind: timestamp,
              class: event_time, supported_grains: [day, month], default: true}
          measures:
            meal_count: {label: Meals, kind: entity_count, entity_key: meal_id,
              accumulation: {kind: event}}
        """,
}

ROLE = "dimension.crew_crew_assignment_crew_role"
EMPLOYEE = "dimension.crew_person_is_employee"
CITY = "dimension.crew_airport_city"
LEG = "dimension.crew_leg_id"

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
    (pkg / "data").mkdir(parents=True)
    (pkg / "models").mkdir()
    (pkg / "data" / "seed.sql").write_text(SEED_SQL + extra_seed)
    (pkg / "package.yml").write_text(PACKAGE)
    (pkg / "graph.yml").write_text(GRAPH)
    for name, body in MODELS.items():
        (pkg / "models" / f"{name}.yml").write_text(textwrap.dedent(body))
    return pkg


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _write_package(tmp_path_factory.mktemp("lookups"))


@pytest.fixture(scope="module")
def runtime(package: Path):
    runtime = Runtime.from_path(str(package))
    yield runtime
    runtime.close()


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
    runtime = Runtime.from_path(
        str(_write_package(tmp_path_factory.mktemp("null_city"), NULL_CITY_SEED))
    )
    yield runtime
    runtime.close()


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
    runtime = Runtime.from_path(
        str(_write_package(tmp_path_factory.mktemp("orphan_leg"), ORPHAN_LEG_SEED))
    )
    yield runtime
    runtime.close()


@pytest.fixture(scope="module")
def gold():
    connection = duckdb.connect()
    connection.execute(SEED_SQL)

    def run(sql: str) -> dict[Any, float]:
        return {row[0]: float(row[1]) for row in connection.execute(sql).fetchall()}

    yield run
    connection.close()


def _ask(runtime: Runtime, measure: str, *, group_by: str = "", where=()) -> dict[Any, float]:
    query: dict[str, Any] = {
        "version": 1,
        "select": [{"as": "value", "expression": {"measure": f"measure.crew.{measure}"}}],
        "where": list(where),
    }
    if group_by:
        query["group_by"] = [group_by]
    rows = runtime.query(query)["rows"]
    return {row.get(group_by) if group_by else None: float(row["value"]) for row in rows}


def _conversion_rate(
    runtime: Runtime, properties: tuple[str, ...] = (), **extra: Any
) -> list[dict[str, Any]]:
    expression = {
        **({"constant_properties": list(properties)} if properties else {}),
        "kind": "conversion",
        "entity": "entity.crew_person",
        "window": {"unit": "day", "value": 1},
        "matching_mode": "first_converted_after_base",
        "base": {"kind": "aggregate", "measure": "measure.crew.checkin_count"},
        "converted": {"kind": "aggregate", "measure": "measure.crew.boarding_count"},
    }
    query = {"version": 1, "select": [{"as": "rate", "expression": expression}], **extra}
    return runtime.query(query)["rows"]


def test_null_and_orphan_foreign_keys_group_under_null(runtime, gold):
    by_employee = _ask(runtime, "fare", group_by=EMPLOYEE)

    assert by_employee == {False: 370.0, True: 130.0, None: 130.0}  # boardings 7 and 8
    assert by_employee == gold(f"SELECT {SQL_EMPLOYEE}, SUM(b.fare) FROM boardings AS b GROUP BY 1")


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
    not_crew = [{"field": ROLE, "op": "IS NULL"}]

    per_leg = _ask(runtime, "boarding_count", group_by=LEG, where=not_crew)

    assert per_leg == {"L1": 2, "L2": 2, "L3": 2, "L9": 1, None: 2}
    assert per_leg == gold(
        "SELECT b.leg_id, COUNT(*) FROM boardings AS b WHERE NOT EXISTS (SELECT 1 FROM"
        " crew_roster AS r WHERE r.leg_id = b.leg_id AND r.person_id = b.person_id) GROUP BY 1"
    )
    assert _ask(runtime, "boarding_count", where=not_crew) == {None: 9}


@pytest.mark.parametrize(
    ("op", "value", "sql", "expected"),
    [
        pytest.param("IS NULL", None, "IS NULL", 9, id="is-null"),
        pytest.param("IS NOT NULL", None, "IS NOT NULL", 4, id="is-not-null"),
        pytest.param("=", "operating", "= 'operating'", 3, id="equals"),
        # A row with no roster match has no role: like a NULL role, it isn't "not operating".
        pytest.param("!=", "operating", "<> 'operating'", 1, id="not-equals"),
        pytest.param(
            "NOT IN", ["operating", "deadhead"], "NOT IN ('operating', 'deadhead')", 0, id="not-in"
        ),
    ],
)
def test_filters_on_a_looked_up_column_follow_sql_null_rules(
    runtime, gold, op, value, sql, expected
):
    where = [{"field": ROLE, "op": op, **({"value": value} if value is not None else {})}]

    got = _ask(runtime, "boarding_count", where=where).get(None, 0.0)

    assert got == expected
    reference = gold(f"SELECT NULL, COUNT(*) FROM boardings AS b WHERE {SQL_ROLE} {sql}")
    assert got == reference.get(None, 0.0)


# Check-ins that lead to a boarding by the same person within a day. Check-ins 2 (no person)
# and 6 (a person with no record) have no person to match on, so they take no part; 1, 5, 7
# and 8 convert, 3 and 4 don't.
CONVERSION_SQL = """
SELECT {group}, AVG(CASE WHEN EXISTS (
  SELECT 1 FROM boardings AS b WHERE b.person_id = c.person_id
  AND b.boarded_at >= c.checked_in_at AND b.boarded_at < c.checked_in_at + INTERVAL 1 DAY
  {same_city}
) THEN 1.0 ELSE 0.0 END)
FROM checkins AS c WHERE c.person_id IN (SELECT person_id FROM people) {where}
GROUP BY 1
"""
SQL_CHECKIN_EMPLOYEE = "(SELECT p.is_employee FROM people AS p WHERE p.person_id = c.person_id)"
# The airport city, read through the leg: NULL for a check-in or boarding whose leg (or the
# leg's airport) has no record.
SQL_CHECKIN_CITY = (
    "(SELECT a.city FROM legs AS l, airports AS a"
    " WHERE l.leg_id = c.leg_id AND a.airport_code = l.airport_code)"
)
SQL_SAME_CITY = f"AND {SQL_CITY} = {SQL_CHECKIN_CITY}"


def _conversion_gold(gold, *, group="NULL", same_city="", where=""):
    sql = CONVERSION_SQL.format(group=group, same_city=same_city, where=where)
    return gold(sql)


def test_conversion_does_not_pair_events_that_have_no_match_entity(runtime, gold):
    """Events pair on the person key with null-safe equality, so an event whose person lookup
    found no row must not pair with another such event (check-in 2 with boarding 7, 6 with 8)."""
    (row,) = _conversion_rate(runtime)
    assert row["rate"] == pytest.approx(4 / 6)
    assert row["rate"] == pytest.approx(_conversion_gold(gold)[None])

    by_employee = _conversion_rate(runtime, group_by=[EMPLOYEE])
    rates = {row[EMPLOYEE]: row["rate"] for row in by_employee}
    assert rates == {False: pytest.approx(0.75), True: pytest.approx(0.5)}  # 1, 7, 8 of 1, 3, 7, 8
    assert rates == pytest.approx(_conversion_gold(gold, group=SQL_CHECKIN_EMPLOYEE))


def test_conversion_does_not_pair_events_on_a_property_that_has_no_match(runtime, gold):
    """Events also pair on constant properties, here the airport city read through the leg.
    Check-in 7 and boarding 11 (leg L9) and check-in 5 and boarding 9 (an airport with no
    record) both have no city, so they must not pair as having "the same city": only check-ins
    1 and 8 have one, and both convert."""
    (row,) = _conversion_rate(runtime, (CITY,))

    assert row["rate"] == pytest.approx(1.0)
    reference = _conversion_gold(
        gold, same_city=SQL_SAME_CITY, where=f"AND {SQL_CHECKIN_CITY} IS NOT NULL"
    )
    assert reference[None] == pytest.approx(1.0)


def test_conversion_pairs_on_a_matched_lookup_whose_property_is_null(null_city_runtime):
    """The lookup matched but the property is NULL, as it is on the event's own entity: check-in
    9 pairs with boarding 14 (both at an airport with no city) and not with boarding 15 (Chicago).
    Check-ins 5 and 7, whose lookups found nothing, still take no part."""
    connection = duckdb.connect()
    connection.execute(SEED_SQL + NULL_CITY_SEED)
    # A leg whose airport row exists: its city, NULL or not, is a match.
    matched = "SELECT l.leg_id FROM legs AS l JOIN airports AS a ON a.airport_code = l.airport_code"
    reference = connection.execute(
        f"""
        SELECT AVG(CASE WHEN EXISTS (
          SELECT 1 FROM boardings AS b WHERE b.person_id = c.person_id
          AND b.boarded_at >= c.checked_in_at AND b.boarded_at < c.checked_in_at + INTERVAL 1 DAY
          AND b.leg_id IN ({matched}) AND {SQL_CITY} IS NOT DISTINCT FROM {SQL_CHECKIN_CITY}
        ) THEN 1.0 ELSE 0.0 END)
        FROM checkins AS c WHERE c.leg_id IN ({matched})
          AND c.person_id IN (SELECT person_id FROM people)
        """
    ).fetchone()
    connection.close()

    (row,) = _conversion_rate(null_city_runtime, (CITY,))

    assert row["rate"] == pytest.approx(0.75)  # check-ins 1, 8 and 9 convert, 10 doesn't
    assert reference is not None and reference[0] == pytest.approx(row["rate"])


def test_conversion_predicate_does_not_qualify_rows_without_its_entity(runtime, gold):
    """Legs with two boardings or more: L1, L2 and L3. Boardings 12 and 13 (no leg) must not
    qualify the check-ins without a leg (3) or with a leg that has no record (4)."""
    busy_legs = {
        "expression": {
            "kind": "metric_predicate",
            "entity": "entity.crew_leg",
            "scope_mode": "entity_only",
            "input": {"measure": "measure.crew.boarding_count"},
            "op": ">=",
            "value": 2,
        },
        "op": "=",
        "value": True,
    }

    (row,) = _conversion_rate(runtime, metric_filters=[busy_legs])

    busy = "SELECT leg_id FROM boardings GROUP BY leg_id HAVING COUNT(*) >= 2"
    reference = _conversion_gold(gold, where=f"AND c.leg_id IN ({busy})")[None]
    assert row["rate"] == pytest.approx(1.0)  # check-ins 1, 5 and 8 qualify, and convert
    assert reference == pytest.approx(row["rate"])


# ClickHouse reads '' or 0 (not NULL) from an unmatched outer-join column unless it is
# Nullable, so its lookups stay inner joins and drop the rows they find no match for.
JOIN_TYPE = {
    "duckdb": "LEFT",
    "motherduck": "LEFT",
    "ducklake": "LEFT",
    "postgres": "LEFT",
    "snowflake": "LEFT",
    "bigquery": "LEFT",
    "databricks": "LEFT",
    "athena": "LEFT",
    "clickhouse": "INNER",
}


def test_every_warehouse_has_a_lookup_join_type():
    assert set(JOIN_TYPE) == set(_WAREHOUSE_CONNECTORS)


@pytest.mark.parametrize("warehouse", sorted(JOIN_TYPE))
def test_the_dialect_decides_the_lookup_join_type(package, warehouse):
    base = load_package_config(str(package))
    config = dataclasses.replace(
        base, package=dataclasses.replace(base.package, warehouse=warehouse)
    )
    query = {
        "version": 1,
        "select": [{"as": "value", "expression": {"measure": "measure.crew.boarding_count"}}],
        "group_by": [CITY],
        "where": [{"field": ROLE, "op": "IS NULL"}],
    }

    sql = " ".join(compile_query(config, Registry(config), query)["sql"].split())

    # The roster, the leg and the airport: three lookup hops.
    assert re.findall(r"\b(\w+) JOIN\b", sql) == [JOIN_TYPE[warehouse]] * 3


BOARDED_MONTH = "temporal_role.crew_boarding_boarded_at__month"
BOARDED_MONTHLY = {"temporal_role": "temporal_role.crew_boarding_boarded_at", "grain": "month"}


def _busy_leg(measure: str, boardings: int) -> dict[str, Any]:
    return {
        "measure": f"measure.crew.{measure}",
        "entity": "entity.crew_leg",
        "op": ">=",
        "value": boardings,
    }


def _scoped(measure: str, *predicates: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "scoped_aggregate",
        "measure": f"measure.crew.{measure}",
        "aggregation": "count_distinct",
        "predicates": list(predicates),
    }


def _monthly(runtime: Runtime, expression: dict[str, Any], *, group_by=()) -> list[dict[str, Any]]:
    query = {
        "version": 1,
        "select": [{"as": "value", "expression": expression}],
        "time": BOARDED_MONTHLY,
        **({"group_by": list(group_by)} if group_by else {}),
    }
    return runtime.query(query)["rows"]


def test_a_metric_filter_keeps_a_row_whose_context_entity_has_no_match(runtime, gold):
    """Boardings whose leg has any boarding that month, grouped by the boarder's kind: the
    person is a context entity of the filter. Boarding 7 (no person) and 8 (a person with no
    record) qualify on their own leg and must stay, under NULL, so the grouped rows add up to
    the ungrouped total (boardings 12 and 13 have no leg, so no filter row applies to them)."""
    expression = _scoped("boarding_count", _busy_leg("boarding_count", 1))

    total = sum(row["value"] for row in _monthly(runtime, expression))
    grouped = _monthly(runtime, expression, group_by=[EMPLOYEE])

    by_employee: dict[Any, int] = {}
    for row in grouped:
        by_employee[row[EMPLOYEE]] = by_employee.get(row[EMPLOYEE], 0) + row["value"]
    assert total == 11
    assert by_employee == {False: 4, True: 5, None: 2}
    assert by_employee == gold(
        f"SELECT {SQL_EMPLOYEE}, COUNT(*) FROM boardings AS b WHERE b.leg_id IS NOT NULL GROUP BY 1"
    )


def test_an_entity_set_ratio_does_not_qualify_rows_without_the_predicate_entity(runtime):
    """Of each month's boardings on a leg, the share on a leg with two boardings or more that
    month. Boardings 12 and 13 have no leg, so no set qualifies them: March has no row, where a
    NULL key in the set would give it the share 1.0."""
    measure = "boarding_population"  # a population count takes the anchored entity-set path
    expression = {
        "kind": "ratio",
        "numerator": _scoped(measure, _busy_leg(measure, 1), _busy_leg(measure, 2)),
        "denominator": _scoped(measure, _busy_leg(measure, 1)),
    }

    rows = _monthly(runtime, expression)

    months = {row[BOARDED_MONTH].month: row["value"] for row in rows}
    # January: legs L1 (4) and L2 (3), all qualify. February: L3 (3) qualifies, L9 (1) doesn't.
    assert months == {1: pytest.approx(7 / 7), 2: pytest.approx(3 / 4)}


def test_a_metric_filter_context_read_through_a_lookup_does_not_pair_with_a_null_group(
    orphan_leg_runtime,
):
    """Meals per leg, for people with exactly two boardings on that leg. The meal is two hops
    from the leg, so its leg is read through the leg table. Meal 1 is on boarding 22 (leg L8, no
    record): it reads NULL there, and must not pair with P8's two boardings with no leg, which
    the filter's set holds under NULL. Only P10's meal on L1 qualifies."""
    query = {
        "version": 1,
        "select": [{"as": "value", "expression": {"measure": "measure.crew.meal_count"}}],
        "group_by": [LEG],
        "metric_filters": [
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.crew_person",
                    "scope_mode": "contextual",
                    "input": {"measure": "measure.crew.boarding_count"},
                    "op": "=",
                    "value": 2,
                },
                "op": "=",
                "value": True,
            }
        ],
    }

    rows = orphan_leg_runtime.query(query)["rows"]

    assert {row[LEG]: row["value"] for row in rows} == {"L1": 1}
