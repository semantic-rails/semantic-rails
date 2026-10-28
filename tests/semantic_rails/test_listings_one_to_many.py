"""Listings through one-to-many routes: a call of dimensions alone answers as distinct rows.

A campus: departments have org units, org units have rooms, rooms are in buildings, and a
building has several addresses. Every relationship is declared child to parent, so a
department reaches its buildings only through one-to-many hops (department -> org units ->
rooms -> building). A call with no measure, metric or time lists the distinct rows its
dimensions take along that route, starting at the first entity it reads, and says which route
it read (``LISTED_THROUGH``). A call with a measure keeps every fan-out guard it has.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.compiler_parts import binding
from semantic_rails.config import load_package_config
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.request_context import context_from_policy_context
from semantic_rails.runtime import Runtime
from semantic_rails.visible_view import hidden_object_ids
from tests.semantic_rails.hidden_absent import (
    CALLER,
    ELIGIBLE,
    absent,
    access_policy,
    envelope,
    visibility_policy,
    with_policies,
)

SEED = """
CREATE TABLE department AS SELECT * FROM (VALUES
 (1, 'Physics'), (2, 'History'), (3, 'Music')) t(dept_id, department_name);
CREATE TABLE org_unit AS SELECT * FROM (VALUES
 (11, 1, 'Physics Lab'), (12, 1, 'Physics Office'), (21, 2, 'History Office')
) t(org_unit_id, dept_id, unit_name);
CREATE TABLE room AS SELECT * FROM (VALUES
 (1, 11, 100, 'R-101', 'public', 30), (2, 12, 100, 'R-102', 'staff', 20),
 (3, 21, 200, 'R-201', 'staff', 50)
) t(room_id, org_unit_id, building_id, room_code, access, capacity);
CREATE TABLE building AS SELECT * FROM (VALUES
 (100, 'B-1'), (200, 'B-2'), (300, 'B-3')) t(building_id, building_key);
CREATE TABLE building_address AS SELECT * FROM (VALUES
 (1, 100, 'Springfield'), (2, 100, 'Shelbyville'), (3, 200, 'Springfield'),
 (4, 300, 'Ogdenville')) t(address_id, building_id, city);
CREATE TABLE office AS SELECT * FROM (VALUES (1, 1, 300), (2, 3, 200)) t(office_id, dept_id, building_id);
"""
DEPARTMENT, UNIT, ROOM = "entity.campus_department", "entity.campus_org_unit", "entity.campus_room"
BUILDING, ADDRESS = "entity.campus_building", "entity.campus_building_address"
DEPARTMENT_NAME = "dimension.campus_department_name"
BUILDING_KEY = "dimension.campus_building_building_key"
CITY = "dimension.campus_building_address_city"
ROOM_CODE = "dimension.campus_room_room_code"
UNIT_DEPARTMENT, ROOM_UNIT = "relationship.unit_department", "relationship.room_unit"
ROOM_BUILDING, ADDRESS_BUILDING = "relationship.room_building", "relationship.address_building"
OFFICE_DEPARTMENT, OFFICE_BUILDING = (
    "relationship.office_department",
    "relationship.office_building",
)
BUILDINGS_ROUTE = [UNIT_DEPARTMENT, ROOM_UNIT, ROOM_BUILDING]
ADDRESSES_ROUTE = [*BUILDINGS_ROUTE, ADDRESS_BUILDING]
ACCESS = {
    "id": "policy.campus.room_access",
    "kind": "row_filter",
    "dimension": "dimension.campus_room_access",
    "attribute": "access",
    "audiences": ["visitor"],
}
OWN_DEPARTMENTS = {
    "id": "policy.campus.own_departments",
    "kind": "row_filter",
    "dimension": DEPARTMENT_NAME,
    "attribute": "departments",
    "audiences": ["visitor"],
}
# Each department's cities through its rooms' buildings, every department kept (the rooms a
# caller sees in place of ``room``): the reference.
CITIES_SQL = """
SELECT DISTINCT d.department_name, a.city
FROM department d
LEFT JOIN org_unit u ON u.dept_id = d.dept_id
LEFT JOIN {rooms} r ON r.org_unit_id = u.org_unit_id
LEFT JOIN building b ON b.building_id = r.building_id
LEFT JOIN building_address a ON a.building_id = b.building_id
"""


def _put(root: Path, name: str, doc: dict[str, Any]) -> None:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _write_package(root: Path, *, traversal: list[str], offices: bool = False) -> Path:
    """The campus package: every relationship child to parent, walkable only as ``traversal``
    says; ``offices`` adds a second link between departments and buildings."""
    _put(root, "package.yml", {
        "schema_version": 1,
        "package": {"id": "campus", "namespace": "campus", "name": "campus",
                    "description": "Departments, rooms and buildings.", "warehouse": "duckdb",
                    "default_db": "campus.duckdb", "seed": {"kind": "external"}},
        "defaults": {"relationship": {"traversal": traversal},
                     "dimension": {"groupable": True, "filterable": True}},
    })  # fmt: skip
    keys = {"department": "dept_id", "org_unit": "org_unit_id", "room": "room_id",
            "building": "building_id", "building_address": "address_id", "office": "office_id"}  # fmt: skip
    if not offices:
        keys.pop("office")
    edges = {
        "unit_department": ("org_unit", "department", "dept_id"),
        "room_unit": ("room", "org_unit", "org_unit_id"),
        "room_building": ("room", "building", "building_id"),
        "address_building": ("building_address", "building", "building_id"),
        **(
            {
                "office_department": ("office", "department", "dept_id"),
                "office_building": ("office", "building", "building_id"),
            }
            if offices
            else {}
        ),
    }
    _put(root, "graph.yml", {"graph": {
        "entities": {
            name: {"label": name.replace("_", " ").title(), "key": [key], "model": name}
            for name, key in keys.items()
        },
        "relationships": {
            rel: {"as": f"relationship.{rel}", "entities": [source, target],
                  "cardinality": "many_to_one", "via": [column], "target": [column]}
            for rel, (source, target, column) in edges.items()
        },
    }})  # fmt: skip
    count = {"kind": "entity_count", "value_type": "count"}
    dimensions = {
        "department": {"name": {"column": "department_name", "kind": "categorical"}},
        "org_unit": {"unit_name": {"kind": "categorical"}},
        "room": {"room_code": {"kind": "categorical"}, "access": {"kind": "categorical"}},
        "building": {"building_key": {"kind": "categorical"}},
        "building_address": {"city": {"kind": "categorical"}},
        "office": {"office_code": {"column": "office_id", "kind": "categorical"}},
    }
    for name, key in keys.items():
        measures: dict[str, Any] = {f"{name}_count": {**count, "entity_key": key}}
        if name == "room":
            # A room's capacity: one number per room, never summed across rooms.
            measures["capacity"] = {"kind": "aggregate", "expr": "capacity", "additive": False}
        _put(root, f"models/{name}.yml", {"model": {
            "id": name, "relation": name, "entities": {name: {}},
            "dimensions": dimensions[name], "measures": measures,
        }})  # fmt: skip
    with duckdb.connect(str(root / "campus.duckdb")) as connection:
        connection.execute(SEED)
    return root


@pytest.fixture(params=[["forward"], ["forward", "reverse"]], ids=["forward_only", "both_ways"])
def package(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    return _write_package(tmp_path / "campus", traversal=request.param)


@pytest.fixture
def runtime(package: Path):
    runtime = Runtime.from_path(str(package))
    yield runtime
    runtime.close()


def _rows(out: dict[str, Any], *columns: str) -> list[tuple[Any, ...]]:
    return sorted((tuple(row[column] for column in columns) for row in out["rows"]), key=repr)


def _reference(runtime: Runtime, sql: str) -> list[tuple[Any, ...]]:
    adapter = runtime._get_adapter()  # noqa: SLF001 - the reference shares the connection
    return sorted((tuple(row) for row in adapter._db.conn.execute(sql).fetchall()), key=repr)  # noqa: SLF001


def _listed(out: dict[str, Any]) -> dict[str, Any]:
    (note,) = [row for row in out.get("assumptions", []) if row["code"] == "LISTED_THROUGH"]
    return note


def _refusal(call: Any) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as caught:
        call()
    return caught.value


def _as_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind as before listings: a call no lookup route answers keeps its own refusal."""
    monkeypatch.setattr(binding, "bind_listing", lambda *args: None)


BY_CITY = {"group_by": [DEPARTMENT_NAME, CITY]}
CITIES = [
    ("History", "Springfield"),
    ("Music", None),
    ("Physics", "Shelbyville"),
    ("Physics", "Springfield"),
]
CITIES_MEANING = (
    "any of the Building Addresses of the Building of any of the Rooms of any of the "
    "Department's Org Units"
)


def test_a_department_lists_the_cities_of_its_rooms_buildings(runtime):
    """Each department with the cities of the buildings its rooms are in: down two one-to-many
    hops, up to the building, down to its addresses. A department with no rooms (Music) lists
    once, with NULL, as a lookup with no match does."""
    out = runtime.query(BY_CITY)
    assert _rows(out, DEPARTMENT_NAME, CITY) == CITIES
    assert _rows(out, DEPARTMENT_NAME, CITY) == _reference(runtime, CITIES_SQL.format(rooms="room"))
    assert not out.get("warnings")


def test_a_listing_was_refused_before(runtime, monkeypatch):
    """Without listings the same call names no route: no start reaches both entities by
    lookups (forward only), or one does only through a one-to-many hop (both ways)."""
    _as_main(monkeypatch)
    refused = _refusal(lambda: runtime.query(BY_CITY))
    assert refused.code in {"PATH_NOT_FOUND", "MIXED_GRAIN_INVALID"}


def test_the_answer_names_each_hop_of_its_route(runtime):
    note = _listed(runtime.query(BY_CITY))
    assert note["message"] == (
        "Listed as distinct rows through one-to-many hops, so nothing is counted: "
        f"{CITIES_MEANING}. Where a Department reaches no row, those columns read NULL."
    )
    assert note["routes"] == [
        {"from": DEPARTMENT, "to": ADDRESS, "via": ADDRESSES_ROUTE, "meaning": CITIES_MEANING}
    ]
    # The same at every verbosity, and on validate (a dry run states what the answer would).
    assert _listed(runtime.query(BY_CITY, verbosity="full")) == note
    assert "routes" not in runtime.query(BY_CITY)  # one route: nothing was chosen
    assert _listed(runtime.validate(BY_CITY)) == note


def test_a_listing_reads_distinct_rows_and_keeps_each_start_row(runtime):
    sql = " ".join(runtime.compile(BY_CITY)["rendered_sql"].split())
    assert "FROM department LEFT JOIN org_unit " in sql
    for table in ("room", "building", "building_address"):
        assert f"LEFT JOIN {table} " in sql
    assert "GROUP BY department.department_name, building_address.city" in sql


def test_a_room_lists_every_address_of_its_building(runtime):
    """Forward to the room's building, then one reverse hop to the building's addresses: one
    distinct row per address."""
    query = {
        "group_by": [ROOM_CODE, BUILDING_KEY, CITY],
        "where": [{"field": ROOM_CODE, "op": "is", "value": "R-101"}],
    }
    out = runtime.query(query)
    assert _rows(out, ROOM_CODE, BUILDING_KEY, CITY) == [
        ("R-101", "B-1", "Shelbyville"),
        ("R-101", "B-1", "Springfield"),
    ]
    assert _listed(out)["routes"] == [
        {
            "from": ROOM,
            "to": ADDRESS,
            "via": [ROOM_BUILDING, ADDRESS_BUILDING],
            "meaning": "any of the Building Addresses of the Room's Building",
        }
    ]


def test_a_listing_starts_at_the_first_entity_it_reads(runtime):
    """Whichever end a filter is on, the listing starts at the first entity the call groups
    by: the rows are the same distinct rows either way, read along one route."""
    by_department = {
        "group_by": [DEPARTMENT_NAME, BUILDING_KEY, CITY],
        "where": [{"field": DEPARTMENT_NAME, "op": "contains", "value": "Physics"}],
    }
    out = runtime.query(by_department)
    assert _rows(out, DEPARTMENT_NAME, BUILDING_KEY, CITY) == [
        ("Physics", "B-1", "Shelbyville"),
        ("Physics", "B-1", "Springfield"),
    ]
    note = _listed(out)
    assert [(route["to"], route["via"]) for route in note["routes"]] == [
        (BUILDING, BUILDINGS_ROUTE),
        (ADDRESS, ADDRESSES_ROUTE),
    ]
    assert CITIES_MEANING in note["message"]  # the longest route names the shorter one too
    by_city = {**by_department, "where": [{"field": CITY, "op": "is", "value": "Springfield"}]}
    out = runtime.query(by_city)
    assert _rows(out, DEPARTMENT_NAME, BUILDING_KEY, CITY) == [
        ("History", "B-2", "Springfield"),
        ("Physics", "B-1", "Springfield"),
    ]
    assert {route["from"] for route in _listed(out)["routes"]} == {DEPARTMENT}


def test_two_one_to_many_routes_ask_which_one_the_question_means(tmp_path):
    """Departments reach buildings through their rooms and through their offices: which
    cities "a department's cities" means is a business definition, so the call asks and never
    runs either route. Each option's own call answers by its route."""
    runtime = Runtime.from_path(str(_write_package(tmp_path, traversal=["forward"], offices=True)))
    try:
        refused = _refusal(lambda: runtime.query(BY_CITY))
        assert refused.code == "AMBIGUOUS_PATH"
        clarification = refused.details["clarification"]
        assert clarification["question"] == (
            "Which Building Address does the question mean for a Department?"
        )
        routes = [option["relationship_path"] for option in clarification["options"]]
        offices = [OFFICE_DEPARTMENT, OFFICE_BUILDING, ADDRESS_BUILDING]
        assert routes == [offices, ADDRESSES_ROUTE]
        rows = {}
        for path in routes:
            chosen = {**BY_CITY, "routes": [{"from": DEPARTMENT, "to": ADDRESS, "via": path}]}
            out = runtime.query(chosen)
            rows[tuple(path)] = _rows(out, DEPARTMENT_NAME, CITY)
            assert _listed(out)["routes"][0]["via"] == path
            # The other option is one switch away, and never runs.
            (route,) = out["routes"]
            assert [other["via"] for other in route["other_routes"]] == [
                other for other in routes if other != path
            ]
        assert rows == {
            tuple(offices): [
                ("History", None),
                ("Music", "Springfield"),
                ("Physics", "Ogdenville"),
            ],
            tuple(ADDRESSES_ROUTE): CITIES,
        }
    finally:
        runtime.close()


def test_a_measure_in_the_call_keeps_its_guards(runtime, monkeypatch):
    """The listing answers; the same grouping with a count of departments (each department
    counted once per address its rooms reach) or a count of rooms by city, or a room's
    non-additive capacity per building (summed across rooms), is refused exactly as before
    listings."""
    assert runtime.query(BY_CITY)["rows"]
    calls = [
        {**BY_CITY, "select": ["measure.campus.department_count"]},
        {"select": ["measure.campus.room_count"], "group_by": [CITY]},
        {"select": ["measure.campus.capacity"], "group_by": [BUILDING_KEY]},
    ]
    listed = [_refusal(lambda call=call: runtime.query(call)) for call in calls]
    _as_main(monkeypatch)
    before = [_refusal(lambda call=call: runtime.query(call)) for call in calls]
    for now, then in zip(listed, before, strict=True):
        assert (now.code, str(now), now.details) == (then.code, str(then), then.details)
    assert listed[2].code == "ROLLUP_UNSAFE"


def _visitor(**attributes: Any) -> RequestContext:
    return RequestContext(actor="end-user", audience="visitor", attributes=attributes)


def test_a_row_filter_on_the_rooms_holds_for_the_listing(package):
    """A visitor sees their own departments and, of the rooms, the ones of their access: each
    department lists the cities of its visible rooms' buildings, and a unit whose rooms are
    all hidden reads as a unit with none. The answer is the one the visitor's visible rows
    give, and a visitor of other access sees other cities."""
    _put(package, "policies.yml", {"semantic_policies": [OWN_DEPARTMENTS, ACCESS]})
    runtime = Runtime.from_path(str(package))
    every = ["Physics", "History", "Music"]
    try:
        for access in ("public", "staff"):
            out = runtime.query(BY_CITY, context=_visitor(access=access, departments=every))
            visible = f"(SELECT * FROM room WHERE access = '{access}')"
            assert _rows(out, DEPARTMENT_NAME, CITY) == _reference(
                runtime, CITIES_SQL.format(rooms=visible)
            )
        public = runtime.query(BY_CITY, context=_visitor(access="public", departments=every))
        assert _rows(public, DEPARTMENT_NAME, CITY) == [
            ("History", None),
            ("Music", None),
            ("Physics", "Shelbyville"),
            ("Physics", "Springfield"),
            ("Physics", None),  # the office's one room is staff only
        ]
        own = runtime.query(BY_CITY, context=_visitor(access="staff", departments=["History"]))
        assert _rows(own, DEPARTMENT_NAME, CITY) == [("History", "Springfield")]
    finally:
        runtime.close()


def test_a_listing_from_an_entity_no_row_filter_reaches_is_held(package):
    """Under a filter on rooms alone, departments are rows no filter reaches: a listing that
    starts at them is held as a listing of departments alone is, never answered around it."""
    _put(package, "policies.yml", {"semantic_policies": [ACCESS]})
    runtime = Runtime.from_path(str(package))
    try:
        visitor = _visitor(access="public")
        listed = _refusal(lambda: runtime.query(BY_CITY, context=visitor))
        alone = _refusal(lambda: runtime.query({"group_by": [DEPARTMENT_NAME]}, context=visitor))
        assert (listed.code, listed.details["reason"]) == (
            "POLICY_DENIED",
            "row_filter_uncovered_root",
        )
        assert (alone.code, alone.details["reason"]) == (listed.code, listed.details["reason"])
        rooms = runtime.query({"group_by": [ROOM_CODE, CITY]}, context=visitor)
        assert _rows(rooms, ROOM_CODE, CITY) == [("R-101", "Shelbyville"), ("R-101", "Springfield")]
    finally:
        runtime.close()


def _execute(package: Path, config: Any, caller: dict[str, Any]) -> dict[str, Any]:
    runtime = Runtime.from_config(config, source_path=str(package))
    try:
        return envelope(
            SemanticLayerMCPAdapter(runtime).call_tool(
                "execute", {"query": BY_CITY}, request_context=context_from_policy_context(caller)
            )
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("action", ["hidden", "visible_only"])
def test_a_route_through_a_hidden_entity_is_no_route(package, action):
    """A caller who can't see rooms gets the response a package without rooms gives: a route
    through an object hidden from the caller is not a route for them, and nothing says it
    exists. A caller who sees rooms gets the listing."""
    config = with_policies(load_package_config(str(package)), visibility_policy(action, ROOM))
    hidden = hidden_object_ids(config, roles=["support"])
    assert ROOM in hidden
    assert _rows(_execute(package, config, ELIGIBLE), DEPARTMENT_NAME, CITY) == CITIES
    refused = _execute(package, config, CALLER)
    assert refused == _execute(package, absent(config, hidden), CALLER)
    assert refused["error"]["code"] in {"PATH_NOT_FOUND", "MIXED_GRAIN_INVALID"}


def test_a_caller_who_cant_see_an_object_off_the_route_gets_the_listing(package):
    """Hiding a room's access level hides nothing the route reads: the caller's view answers
    the listing as the whole package does, and as a package without that dimension does."""
    hidden_dimension = "dimension.campus_room_access"
    config = with_policies(
        load_package_config(str(package)), visibility_policy("hidden", hidden_dimension)
    )
    hidden = hidden_object_ids(config, roles=["support"])
    assert hidden == {hidden_dimension}
    listed = _execute(package, config, CALLER)
    assert _rows(listed, DEPARTMENT_NAME, CITY) == CITIES
    assert listed == _execute(package, absent(config, hidden), CALLER)


@pytest.mark.parametrize("denied", [ROOM, ROOM_UNIT])
def test_a_denied_object_on_the_route_refuses_the_listing(package, denied):
    """An object a policy denies the caller is read by no call of theirs, a listing's route
    included: the call is refused naming it, as a call reading it directly is."""
    config = with_policies(load_package_config(str(package)), access_policy("deny", denied))
    runtime = Runtime.from_config(config, source_path=str(package))
    try:
        refused = _refusal(
            lambda: runtime.query(BY_CITY, context=context_from_policy_context(CALLER))
        )
    finally:
        runtime.close()
    assert (refused.code, str(refused)) == ("POLICY_DENIED", f"{denied} is denied by policy.")
