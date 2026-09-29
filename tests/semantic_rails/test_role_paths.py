"""Two relationships between one pair of entities (role-playing keys).

A leg has an origin and a destination airport, both keys into one airports
table. Neither role is the "right" airport for a question about a leg's city,
so the loader keeps both relationships and a query that could use either one
is refused instead of answered from whichever was declared last. Pinning a
route (``path_preferences`` or a per-relationship ``path_preference``) makes
the query answerable.

Gold values come from plain SQL over the seed, not from the engine.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import duckdb
import pytest

from semantic_rails.config import load_package_config
from semantic_rails.config_validation import _compiled_package_warnings
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime

SEED_SQL = """
CREATE TABLE airports (airport_code VARCHAR, city VARCHAR);
CREATE TABLE legs (leg_id INTEGER, origin_code VARCHAR, destination_code VARCHAR, seats INTEGER);
INSERT INTO airports VALUES
  ('JFK', 'New York'), ('LHR', 'London'), ('LAX', 'Los Angeles'), ('ORD', 'Chicago');
INSERT INTO legs VALUES
  (1, 'ORD', 'JFK', 4),
  (2, 'JFK', 'LHR', 6),
  (3, 'LHR', 'JFK', 5),
  (4, 'JFK', 'LAX', 4);
"""

ORIGIN = "relationship.legs_origin_airport"
DESTINATION = "relationship.legs_destination_airport"
CITY = "dimension.air_airport_city"
SEATS = "measure.air.seats"


def _gold_seats_by_city(role_column: str) -> dict[str, int]:
    con = duckdb.connect(":memory:")
    con.execute(SEED_SQL)
    rows = con.execute(
        f"SELECT a.city, SUM(l.seats) FROM legs l JOIN airports a "
        f"ON a.airport_code = l.{role_column} GROUP BY a.city"
    ).fetchall()
    return {city: int(total) for city, total in rows}


GOLD_BY_ORIGIN = _gold_seats_by_city("origin_code")
GOLD_BY_DESTINATION = _gold_seats_by_city("destination_code")

_RELATIONSHIPS = {
    "origin": (
        "legs_origin_airport",
        ["      via: [origin_code]"],
    ),
    "destination": (
        "legs_destination_airport",
        ["      via: [destination_code]"],
    ),
}


def _write_package(
    root: Path,
    *,
    explicit: tuple[str, ...] = ("origin", "destination"),
    inferred_origin: bool = False,
    origin_preference: int | None = None,
    path_preferences: str = "",
) -> Path:
    """Legs and airports. ``explicit`` lists the roles authored in
    ``graph.relationships`` in declaration order; ``inferred_origin`` instead
    lets the origin come from the leg model's ``entities:`` block."""
    pkg = root / "air"
    (pkg / "data").mkdir(parents=True, exist_ok=True)
    (pkg / "models").mkdir(exist_ok=True)
    (pkg / "data" / "seed.sql").write_text(SEED_SQL)
    (pkg / "package.yml").write_text(
        textwrap.dedent(
            f"""
            schema_version: 1
            package:
              id: air
              namespace: air
              name: air
              description: Role-playing airport keys.
              warehouse: duckdb
              default_db: {(root / "air.duckdb").as_posix()}
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
    relationship_lines: list[str] = []
    for role in explicit:
        name, extra = _RELATIONSHIPS[role]
        relationship_lines += [
            f"    {name}:",
            f"      id: relationship.{name}",
            "      entities: [leg, airport]",
            "      cardinality: many_to_one",
            *extra,
            "      target: [airport_code]",
        ]
        if role == "origin" and origin_preference is not None:
            relationship_lines.append(f"      path_preference: {origin_preference}")
    graph = textwrap.dedent(
        """\
        graph:
          entities:
            leg: {label: Leg, key: [leg_id], model: legs}
            airport: {label: Airport, key: [airport_code], model: airports}
        """
    )
    if relationship_lines:
        graph += "  relationships:\n" + "\n".join(relationship_lines) + "\n"
    graph += path_preferences
    (pkg / "graph.yml").write_text(graph)

    origin_entity = "    airport: {expr: origin_code}\n" if inferred_origin else ""
    (pkg / "models" / "legs.yml").write_text(
        "model:\n"
        "  id: legs\n"
        "  relation: legs\n"
        "  entities:\n"
        "    leg: {}\n" + origin_entity + "  measures:\n"
        "    seats:\n"
        "      kind: aggregate\n"
        "      label: Seats\n"
        "      expr: seats\n"
        "      accumulation: {kind: flow}\n"
        "      value_type: count\n"
    )
    (pkg / "models" / "airports.yml").write_text(
        textwrap.dedent(
            """
            model:
              id: airports
              relation: airports
              entities:
                airport: {}
              dimensions:
                city:
                  as: dimension.air_airport_city
                  label: City
                  kind: categorical
            """
        )
    )
    return pkg


@pytest.fixture(autouse=True)
def _allow_external_package_paths(monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def _by_city_query() -> dict:
    return {
        "version": 1,
        "select": [{"expression": {"measure": SEATS}, "as": "seats"}],
        "group_by": [CITY],
    }


def _seats_by_city(runtime: Runtime) -> dict[str, int]:
    rows = runtime.query(_by_city_query())["rows"]
    return {row[CITY]: int(row["seats"]) for row in rows}


def _relationship_ids(config, source: str, target: str) -> list[str]:
    return sorted(
        rel.id
        for rel in config.relationships
        if rel.source_entity == source and rel.target_entity == target
    )


AUTHORED_LAYOUTS = {
    "explicit_origin_first": {"explicit": ("origin", "destination")},
    "explicit_destination_first": {"explicit": ("destination", "origin")},
    "inferred_origin_explicit_destination": {
        "explicit": ("destination",),
        "inferred_origin": True,
    },
}


@pytest.mark.parametrize("layout", AUTHORED_LAYOUTS)
def test_loader_keeps_both_roles(tmp_path, layout):
    config = load_package_config(str(_write_package(tmp_path, **AUTHORED_LAYOUTS[layout])))
    relationships = _relationship_ids(config, "entity.air_leg", "entity.air_airport")
    assert len(relationships) == 2
    assert DESTINATION in relationships
    columns = {
        rel.id: rel.source_columns
        for rel in config.relationships
        if rel.source_entity == "entity.air_leg"
    }
    assert columns[DESTINATION] == ["destination_code"]
    assert ["origin_code"] in columns.values()


@pytest.mark.parametrize("layout", AUTHORED_LAYOUTS)
def test_query_through_two_roles_is_refused_whatever_the_declaration_order(tmp_path, layout):
    config = load_package_config(str(_write_package(tmp_path, **AUTHORED_LAYOUTS[layout])))
    runtime = Runtime.from_path(str(tmp_path / "air"))
    with pytest.raises(SemanticLayerError) as exc_info:
        runtime.query(_by_city_query())

    err = exc_info.value
    assert err.code == "AMBIGUOUS_PATH"
    candidates = sorted(path[0] for path in err.details["candidates"])
    assert candidates == _relationship_ids(config, "entity.air_leg", "entity.air_airport")
    assert all(len(path) == 1 for path in err.details["candidates"])
    hint = err.details["hint"]
    assert "path_preferences" in hint
    assert "path_preference" in hint
    assert all(rel_id in str(err) for rel_id in candidates)


def test_a_single_authored_role_still_answers(tmp_path):
    _write_package(tmp_path, explicit=("destination",))
    runtime = Runtime.from_path(str(tmp_path / "air"))
    assert _seats_by_city(runtime) == GOLD_BY_DESTINATION


def test_explicit_relationship_on_the_inferred_columns_replaces_it(tmp_path):
    config = load_package_config(
        str(_write_package(tmp_path, explicit=("origin",), inferred_origin=True))
    )
    assert _relationship_ids(config, "entity.air_leg", "entity.air_airport") == [ORIGIN]
    runtime = Runtime.from_path(str(tmp_path / "air"))
    assert _seats_by_city(runtime) == GOLD_BY_ORIGIN


@pytest.mark.parametrize("explicit", [("origin", "destination"), ("destination", "origin")])
@pytest.mark.parametrize(
    ("role", "gold"), [("origin", GOLD_BY_ORIGIN), ("destination", GOLD_BY_DESTINATION)]
)
def test_path_preferences_pin_the_role(tmp_path, explicit, role, gold):
    relationship = ORIGIN if role == "origin" else DESTINATION
    _write_package(
        tmp_path,
        explicit=explicit,
        path_preferences=(
            "  path_preferences:\n"
            "    - source_entity: leg\n"
            "      target_entity: airport\n"
            f"      relationship_path: [{relationship}]\n"
        ),
    )
    runtime = Runtime.from_path(str(tmp_path / "air"))
    assert _seats_by_city(runtime) == gold


def test_relationship_path_preference_pins_the_role(tmp_path):
    _write_package(tmp_path, origin_preference=10)
    runtime = Runtime.from_path(str(tmp_path / "air"))
    assert _seats_by_city(runtime) == GOLD_BY_ORIGIN


def test_load_warns_when_roles_share_an_entity_pair_unpinned(tmp_path):
    pkg = _write_package(tmp_path)
    config = load_package_config(str(pkg))
    warnings = [
        warning
        for warning in _compiled_package_warnings(config, pkg)
        if isinstance(warning, dict) and warning["code"] == "RELATIONSHIP_ROLES_UNPINNED"
    ]
    assert len(warnings) == 1
    details = warnings[0]["details"]
    assert details["source_entity"] == "entity.air_leg"
    assert details["target_entity"] == "entity.air_airport"
    assert sorted(details["relationships"]) == [DESTINATION, ORIGIN]
    assert "path_preferences" in warnings[0]["message"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "path_preferences": (
                "  path_preferences:\n"
                "    - source_entity: leg\n"
                "      target_entity: airport\n"
                f"      relationship_path: [{ORIGIN}]\n"
            )
        },
        {"origin_preference": 10},
        {"explicit": ("destination",)},
    ],
    ids=["pinned_pair", "preferred_relationship", "single_role"],
)
def test_no_load_warning_when_pinned_or_single(tmp_path, kwargs):
    pkg = _write_package(tmp_path, **kwargs)
    config = load_package_config(str(pkg))
    codes = [
        warning["code"]
        for warning in _compiled_package_warnings(config, pkg)
        if isinstance(warning, dict)
    ]
    assert "RELATIONSHIP_ROLES_UNPINNED" not in codes
