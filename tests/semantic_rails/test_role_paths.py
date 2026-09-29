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

import itertools
import textwrap
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from semantic_rails.compiler import _validate_rollup_safety
from semantic_rails.compiler_parts.paths import (
    _direct_dimension_source_expr,
    _direct_entity_key_source_expr,
    _pair_key_routes,
)
from semantic_rails.config import load_package_config, normalize_package
from semantic_rails.config_validation import _compiled_package_warnings
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime

SEED_SQL = """
CREATE TABLE airports (airport_code VARCHAR, city VARCHAR, slots INTEGER, iata VARCHAR);
CREATE TABLE legs (leg_id INTEGER, origin_code VARCHAR, destination_code VARCHAR, seats INTEGER, alternate_code VARCHAR);
INSERT INTO airports VALUES
  ('JFK', 'New York', 10, 'JFK'), ('LHR', 'London', 8, 'LHR'),
  ('LAX', 'Los Angeles', 3, 'LAX'), ('ORD', 'Chicago', 2, 'ORD');
INSERT INTO legs VALUES
  (1, 'ORD', 'JFK', 4, 'LAX'),
  (2, 'JFK', 'LHR', 6, 'ORD'),
  (3, 'LHR', 'JFK', 5, 'ORD'),
  (4, 'JFK', 'LAX', 4, 'LHR');
"""

ORIGIN = "relationship.legs_origin_airport"
DESTINATION = "relationship.legs_destination_airport"
CITY = "dimension.air_airport_city"
CODE = "dimension.air_airport_code"
SEATS = "measure.air.seats"
SLOTS = "measure.air.airport_slots"


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


def _gold_rows(select: str, where: str, role_column: str) -> list[tuple]:
    con = duckdb.connect(":memory:")
    con.execute(SEED_SQL)
    group = f"GROUP BY {select}" if select else ""
    head = f"{select}, " if select else ""
    rows = con.execute(
        f"SELECT {head}SUM(l.seats) FROM legs l JOIN airports a "
        f"ON a.airport_code = l.{role_column} WHERE {where} {group}"
    ).fetchall()
    return sorted((*row[:-1], int(row[-1])) for row in rows)


def _seats_query(**parts) -> dict:
    return {
        "version": 1,
        "select": [{"expression": {"measure": SEATS}, "as": "seats"}],
        **parts,
    }


_LARGE_AIRPORT = {
    "kind": "metric_predicate",
    "entity": "entity.air_airport",
    "scope_mode": "entity_only",
    "input": {"measure": SLOTS},
    "op": ">=",
    "value": 10,
}

# Every way a query reaches the airport, the key dimension included:
# (query, the columns each result row is read as, gold SELECT list, gold WHERE).
# The key dimension is readable from the leg's own foreign key, which must not pick a role.
ROLE_QUERIES = {
    "group_by_city": (_seats_query(group_by=[CITY]), [CITY], "a.city", "TRUE"),
    "group_by_key": (_seats_query(group_by=[CODE]), [CODE], "a.airport_code", "TRUE"),
    "group_by_city_and_key": (
        _seats_query(group_by=[CITY, CODE]),
        [CITY, CODE],
        "a.city, a.airport_code",
        "TRUE",
    ),
    "where_on_key": (
        _seats_query(where=[{"field": CODE, "op": "=", "value": "JFK"}]),
        [],
        "",
        "a.airport_code = 'JFK'",
    ),
    "where_on_city": (
        _seats_query(where=[{"field": CITY, "op": "=", "value": "New York"}]),
        [],
        "",
        "a.city = 'New York'",
    ),
    "metric_predicate_on_airport": (
        _seats_query(metric_filters=[{"expression": _LARGE_AIRPORT, "op": "=", "value": True}]),
        [],
        "",
        "a.slots >= 10",
    ),
    "scoped_aggregate_predicate_on_airport": (
        {
            "version": 1,
            "select": [
                {
                    "as": "seats",
                    "expression": {
                        "kind": "scoped_aggregate",
                        "measure": SEATS,
                        "aggregation": "sum",
                        "predicates": [
                            {
                                "measure": SLOTS,
                                "entity": "entity.air_airport",
                                "op": ">=",
                                "value": 10,
                            }
                        ],
                    },
                }
            ],
        },
        [],
        "",
        "a.slots >= 10",
    ),
}

_RELATIONSHIPS = {
    "origin": (
        "legs_origin_airport",
        ["      via: [origin_code]"],
    ),
    "destination": (
        "legs_destination_airport",
        ["      via: [destination_code]"],
    ),
    "alternate": (
        "legs_alternate_airport",
        ["      via: [alternate_code]"],
    ),
}


def _write_package(
    root: Path,
    *,
    explicit: tuple[str, ...] = ("origin", "destination"),
    inferred_origin: bool = False,
    preferences: dict[str, int] | None = None,
    path_preferences: str = "",
    reverse: tuple[str, ...] = (),
    extra_seed: str = "",
    destination_target: str = "airport_code",
) -> Path:
    """Legs and airports. ``explicit`` lists the roles authored in
    ``graph.relationships`` in declaration order; ``inferred_origin`` instead
    lets the origin come from the leg model's ``entities:`` block.
    ``preferences`` sets a role's relationship ``path_preference``; ``reverse`` lists roles
    declared from the airport's side (``[airport, leg]``); ``extra_seed`` is more seed SQL;
    ``destination_target`` is the airport column the destination role joins to (a column
    other than the key gives a role the key-column filter alone would not see)."""
    preferences = preferences or {}
    pkg = root / "air"
    (pkg / "data").mkdir(parents=True, exist_ok=True)
    (pkg / "models").mkdir(exist_ok=True)
    (pkg / "data" / "seed.sql").write_text(SEED_SQL + extra_seed)
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
        target = destination_target if role == "destination" else "airport_code"
        relationship_lines += [
            f"    {name}:",
            f"      id: relationship.{name}",
            "      entities: [leg, airport]",
            "      cardinality: many_to_one",
            *extra,
            f"      target: [{target}]",
        ]
        if role in preferences:
            relationship_lines.append(f"      path_preference: {preferences[role]}")
    for role in reverse:
        name, _ = _RELATIONSHIPS[role]
        relationship_lines += [
            f"    {name}_reverse:",
            f"      id: relationship.{name}_reverse",
            "      entities: [airport, leg]",
            "      cardinality: one_to_many",
            "      via: [airport_code]",
            f"      target: [{role}_code]",
        ]
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
              measures:
                slots:
                  as: measure.air.airport_slots
                  kind: aggregate
                  label: Slots
                  expr: slots
                  accumulation: {kind: flow}
                  value_type: count
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
    "destination_targets_a_non_key_column": {
        "explicit": ("origin", "destination"),
        "destination_target": "iata",
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


def _pin(role: str, method: str, layout: str) -> dict:
    """``_write_package`` arguments that pin ``role``: a ``path_preferences`` row for the
    pair, or a lower ``path_preference`` on that role's relationship (the inferred origin
    has none, so its pin raises the destination's instead)."""
    if method == "pair":
        inferred = AUTHORED_LAYOUTS[layout].get("inferred_origin")
        origin = "relationship.legs_airport" if inferred else ORIGIN
        relationship = DESTINATION if role == "destination" else origin
        return {
            "path_preferences": (
                "  path_preferences:\n"
                "    - source_entity: leg\n"
                "      target_entity: airport\n"
                f"      relationship_path: [{relationship}]\n"
            )
        }
    return {"preferences": {"destination": 10 if role == "destination" else 200}}


def _rows(runtime: Runtime, query: dict, columns: list[str]) -> list[tuple]:
    rows = runtime.query(query)["rows"]
    return sorted((*(row[column] for column in columns), int(row["seats"])) for row in rows)


@pytest.mark.parametrize("query_name", ROLE_QUERIES)
@pytest.mark.parametrize("layout", AUTHORED_LAYOUTS)
def test_every_query_through_two_roles_is_refused_unpinned(tmp_path, layout, query_name):
    """Neither the key dimension nor a metric predicate is answered from the role
    that happens to be declared first."""
    _write_package(tmp_path, **AUTHORED_LAYOUTS[layout])
    runtime = Runtime.from_path(str(tmp_path / "air"))
    with pytest.raises(SemanticLayerError) as exc_info:
        runtime.query(ROLE_QUERIES[query_name][0])
    assert exc_info.value.code == "AMBIGUOUS_PATH"


@pytest.mark.parametrize("query_name", ROLE_QUERIES)
@pytest.mark.parametrize("method", ["pair", "relationship"])
@pytest.mark.parametrize(
    ("role", "role_column"), [("origin", "origin_code"), ("destination", "destination_code")]
)
@pytest.mark.parametrize("layout", AUTHORED_LAYOUTS)
def test_pinned_role_returns_that_roles_gold_value(
    tmp_path, layout, role, role_column, method, query_name
):
    query, columns, select, where = ROLE_QUERIES[query_name]
    _write_package(tmp_path, **AUTHORED_LAYOUTS[layout], **_pin(role, method, layout))
    runtime = Runtime.from_path(str(tmp_path / "air"))
    gold = _gold_rows(select, where, role_column)
    assert gold
    assert _rows(runtime, query, columns) == gold


@pytest.mark.parametrize("query_name", ROLE_QUERIES)
def test_the_two_roles_have_different_gold_values(query_name):
    """A pin is only proven if the roles disagree on the query."""
    _query, _columns, select, where = ROLE_QUERIES[query_name]
    assert _gold_rows(select, where, "origin_code") != _gold_rows(select, where, "destination_code")


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
    assert details["pair_pinned"] is False
    assert "path_preference" in warnings[0]["message"]


def _role_warnings(tmp_path, **kwargs) -> list[dict]:
    pkg = _write_package(tmp_path, **kwargs)
    config = load_package_config(str(pkg))
    return [
        warning
        for warning in _compiled_package_warnings(config, pkg)
        if isinstance(warning, dict) and warning["code"] == "RELATIONSHIP_ROLES_UNPINNED"
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"preferences": {"origin": 10}},
        {"preferences": {"destination": 10}},
        {"explicit": ("origin", "destination", "alternate"), "preferences": {"alternate": 10}},
        {"explicit": ("destination",)},
    ],
    ids=["origin_lowest", "destination_lowest", "unique_lowest_of_three", "single_role"],
)
def test_no_load_warning_when_one_relationship_is_preferred_or_single(tmp_path, kwargs):
    assert _role_warnings(tmp_path, **kwargs) == []


@pytest.mark.parametrize(
    "preferences",
    [{"origin": 50, "destination": 50}, {"origin": 50, "destination": 50, "alternate": 100}],
    ids=["two_tied", "two_tied_below_a_third"],
)
def test_load_warns_when_the_lowest_path_preference_is_tied(tmp_path, preferences):
    """Ties at the minimum still refuse every query, so they still warn."""
    warnings = _role_warnings(
        tmp_path,
        explicit=("origin", "destination", "alternate"),
        preferences=preferences,
    )
    assert len(warnings) == 1
    assert len(warnings[0]["details"]["relationships"]) == 3


def test_load_warning_for_a_pair_pin_says_what_the_pin_covers(tmp_path):
    """A pair pin covers queries from the source to the target only, so the warning stays
    and does not call the pair pinned."""
    warnings = _role_warnings(tmp_path, **_pin("origin", "pair", "explicit_origin_first"))
    assert len(warnings) == 1
    message = warnings[0]["message"]
    assert warnings[0]["details"]["pair_pinned"] is True
    assert "only queries that start at entity.air_leg and end at entity.air_airport" in message
    assert "lower path_preference" in message
    assert "are pinned" not in message


def test_the_key_shortcut_declines_a_pair_with_several_routes(tmp_path):
    """The direct read of a key never picks a role: a pair with several routes returns None,
    so the caller goes through path selection, which follows a pin or refuses."""
    config = load_package_config(str(_write_package(tmp_path)))
    leg, airport = "entity.air_leg", "entity.air_airport"
    routes = _pair_key_routes(leg, airport, "airport_code", config)
    assert sorted(column for _rel, column in routes) == ["destination_code", "origin_code"]
    assert _direct_entity_key_source_expr(leg, airport, "airport_code", config) is None
    assert _direct_dimension_source_expr(leg, CODE, config) is None


def test_the_key_shortcut_declines_when_another_role_targets_a_non_key_column(tmp_path):
    """Only the origin role reads the key column, so counting key routes alone finds one
    route; the pair still has two pairings, so the shortcut must decline."""
    config = load_package_config(
        str(_write_package(tmp_path, **AUTHORED_LAYOUTS["destination_targets_a_non_key_column"]))
    )
    leg, airport = "entity.air_leg", "entity.air_airport"
    assert [column for _rel, column in _pair_key_routes(leg, airport, "airport_code", config)] == [
        "origin_code"
    ]
    assert _direct_entity_key_source_expr(leg, airport, "airport_code", config) is None
    assert _direct_dimension_source_expr(leg, CODE, config) is None


def test_the_shortcut_still_reads_a_single_role_from_the_source_table(tmp_path):
    config = load_package_config(str(_write_package(tmp_path, explicit=("destination",))))
    expr = _direct_entity_key_source_expr(
        "entity.air_leg", "entity.air_airport", "airport_code", config
    )
    assert expr is not None
    assert expr.parts[-1] == "destination_code"


def _normalized_joins(relationships: dict, inferred: list[str] | None = None) -> dict[str, dict]:
    """The leg model's joins after the loader translates ``graph.relationships``;
    ``inferred`` gives the leg model an ``entities:`` foreign key to the airport."""
    leg_entities: dict = {"leg": {}}
    if inferred:
        leg_entities["airport"] = {"expr": inferred[0] if len(inferred) == 1 else inferred}
    raw = {
        "package": {"namespace": "air"},
        "graph": {
            "entities": {
                "leg": {"key": ["leg_id"], "model": "legs"},
                "airport": {"key": ["airport_code"], "model": "airports"},
            },
            "relationships": relationships,
        },
        "models": {
            "legs": {"id": "legs", "relation": "legs", "entities": leg_entities},
            "airports": {"id": "airports", "relation": "airports", "entities": {"airport": {}}},
        },
    }
    return normalize_package(raw)["models"]["legs"]["joins"]


def _entry(name: str, via: list[str] | None = None, target: list[str] | None = None):
    spec: dict = {"id": f"relationship.{name}", "entities": ["leg", "airport"]}
    if via:
        spec["via"] = via
    if target:
        spec["target"] = target
    return name, spec


def _all_orders_are_refused(entries: list[tuple[str, dict]], inferred: list[str] | None = None):
    for order in itertools.permutations(entries):
        with pytest.raises(SemanticLayerError) as exc_info:
            _normalized_joins(dict(order), inferred)
        assert exc_info.value.code == "INVALID_CONFIG", order
        assert "keep one" in str(exc_info.value), order


def test_two_authored_entries_on_the_same_columns_are_refused_in_every_order():
    """Never merged by id or declaration order, so no authored attribute (a pin, a
    traversal) is dropped without notice."""
    _all_orders_are_refused(
        [
            _entry("a", ["origin_code"]),
            _entry("b", ["destination_code"]),
            _entry("c", ["origin_code"]),
        ]
    )


def test_entries_on_the_same_columns_are_refused_whatever_their_target_columns():
    """An unset ``target`` is the entity key, not a wildcard: entries that name different
    targets, or none, on one column still refuse in every order."""
    _all_orders_are_refused(
        [
            _entry("by_code", ["origin_code"], ["airport_code"]),
            _entry("by_other", ["origin_code"], ["other_code"]),
            _entry("a_default", ["origin_code"]),
        ]
    )


def test_two_entries_that_resolve_to_the_inferred_foreign_key_are_refused():
    """One restates the inferred foreign key with `via`, the other by leaving it unset."""
    _all_orders_are_refused(
        [_entry("first", ["origin_code"]), _entry("second")], inferred=["origin_code"]
    )


@pytest.mark.parametrize("via", [None, ["origin_code"]])
def test_an_explicit_entry_replaces_the_inferred_foreign_key_it_restates(via):
    joins = _normalized_joins(dict([_entry("origin", via)]), inferred=["origin_code"])
    assert [join["id"] for join in joins.values()] == ["relationship.origin"]


def test_an_entry_on_other_columns_is_kept_beside_the_inferred_foreign_key():
    joins = _normalized_joins(
        dict([_entry("destination", ["destination_code"])]), inferred=["origin_code"]
    )
    assert len(joins) == 2
    assert sorted(join.get("via", ["<inferred>"])[0] for join in joins.values()) == [
        "<inferred>",
        "destination_code",
    ]


def test_second_entry_without_source_columns_is_refused_by_name():
    with pytest.raises(SemanticLayerError) as exc_info:
        _normalized_joins(dict([_entry("a", ["origin_code"]), _entry("no_columns")]))
    assert exc_info.value.code == "INVALID_CONFIG"
    assert "relationship.no_columns" in str(exc_info.value)


def test_load_warns_for_roles_declared_from_opposite_sides(tmp_path):
    """A role written as ``[airport, leg]`` is still a second route between the pair."""
    warnings = _role_warnings(tmp_path, explicit=("destination",), reverse=("origin",))
    assert len(warnings) == 1
    assert sorted(warnings[0]["details"]["relationships"]) == [
        DESTINATION,
        "relationship.legs_origin_airport_reverse",
    ]


def test_a_relationship_declared_from_the_other_side_on_the_same_columns_is_one_role(tmp_path):
    assert _role_warnings(tmp_path, explicit=("destination",), reverse=("destination",)) == []


def test_path_preference_zero_pins_a_role(tmp_path):
    """Zero is the lowest preference, not "unset": following the hint with 0 must work."""
    assert _role_warnings(tmp_path, preferences={"origin": 0}) == []
    _write_package(tmp_path, preferences={"origin": 0})
    assert _seats_by_city(Runtime.from_path(str(tmp_path / "air"))) == GOLD_BY_ORIGIN


def test_a_negative_path_preference_is_refused(tmp_path):
    pkg = _write_package(tmp_path, preferences={"origin": -1})
    with pytest.raises(SemanticLayerError) as exc_info:
        load_package_config(str(pkg))
    assert exc_info.value.code == "INVALID_CONFIG"
    assert "path_preference" in str(exc_info.value)


ORPHAN_LEG = "INSERT INTO legs VALUES (5, 'ORD', 'SFO', 7, 'LAX');\n"


def test_a_pinned_role_reads_its_key_through_the_join_so_a_leg_without_an_airport_is_left_out(
    tmp_path,
):
    """Documented behaviour: with several roles the key comes from the pinned relationship's
    join, like any other airport column, so a leg whose code matches no airport row is not
    counted. A single role reads the key from the leg's own column and keeps it."""
    query = _seats_query(group_by=[CODE])
    con = duckdb.connect(":memory:")
    con.execute(SEED_SQL + ORPHAN_LEG)
    joined = sorted(
        (code, int(total))
        for code, total in con.execute(
            "SELECT a.airport_code, SUM(l.seats) FROM legs l JOIN airports a "
            "ON a.airport_code = l.destination_code GROUP BY 1"
        ).fetchall()
    )
    own_column = sorted(
        (code, int(total))
        for code, total in con.execute(
            "SELECT destination_code, SUM(seats) FROM legs GROUP BY 1"
        ).fetchall()
    )
    assert ("SFO", 7) in own_column
    assert ("SFO", 7) not in joined

    _write_package(
        tmp_path / "two", extra_seed=ORPHAN_LEG, **_pin("destination", "relationship", "")
    )
    two_roles = Runtime.from_path(str(tmp_path / "two" / "air"))
    assert _rows(two_roles, query, [CODE]) == joined

    _write_package(tmp_path / "one", explicit=("destination",), extra_seed=ORPHAN_LEG)
    one_role = Runtime.from_path(str(tmp_path / "one" / "air"))
    assert _rows(one_role, query, [CODE]) == own_column


def _config_with_rollup_hints(tmp_path, hints: dict[str, list[str]], *, reverse_order=False):
    """The role package with each relationship's ``rollup_safe_aggregations`` set, and the
    seats measure rolled up to the airport."""
    config = load_package_config(str(_write_package(tmp_path)))
    relationships = [
        replace(rel, rollup_safe_aggregations=hints.get(rel.id, [])) for rel in config.relationships
    ]
    if reverse_order:
        relationships.reverse()
    measure = replace(
        next(m for m in config.measures if m.id == SEATS), aggregation_entity="entity.air_airport"
    )
    measures = [measure, *(m for m in config.measures if m.id != SEATS)]
    return replace(config, relationships=relationships, measures=measures)


def _rollup_is_refused(config, aggregation: str) -> bool:
    bound = [SimpleNamespace(measure_id=SEATS, aggregation=aggregation)]
    try:
        _validate_rollup_safety(bound, config)
    except SemanticLayerError as exc:
        assert exc.code == "ROLLUP_UNSAFE"
        return True
    return False


@pytest.mark.parametrize("reverse_order", [False, True])
def test_rollup_hints_of_a_role_pair_are_intersected_whatever_the_order(tmp_path, reverse_order):
    """Origin allows sum, destination allows sum and max: only sum is allowed by both."""
    config = _config_with_rollup_hints(
        tmp_path, {ORIGIN: ["sum"], DESTINATION: ["sum", "max"]}, reverse_order=reverse_order
    )
    assert not _rollup_is_refused(config, "sum")
    assert _rollup_is_refused(config, "max")


def test_rollup_hints_with_nothing_in_common_allow_no_aggregation(tmp_path):
    """An empty intersection restricts everything; it must not read as "no restriction"."""
    config = _config_with_rollup_hints(tmp_path, {ORIGIN: ["sum"], DESTINATION: ["max"]})
    assert _rollup_is_refused(config, "sum")
    assert _rollup_is_refused(config, "max")
