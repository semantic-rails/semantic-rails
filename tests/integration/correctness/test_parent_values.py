"""A parent's value on its children's rows, on DuckDB and Postgres.

Fixture ``campus/``: a room names its building by its own key and its floor by another, and a
floor names its building; every relationship is declared child to parent and walks forward
only. "Each room with its building's assignable area" groups by a room's columns and selects a
building's measure. From the building's side that is a one-to-many walk the package refuses
(``PATH_NOT_FOUND``), or on a two-way package a question of which route reaches the rooms
(``AMBIGUOUS_PATH``). Read from the rooms' side it is one lookup by the room's own building
key: each building counts once, with its whole value, in every group that holds one of its
rooms; a group whose rooms name no building (R-103) reads NULL, and a room with no building
(R-105, beside two offices with one) adds nothing to its group; and a total is the call
without the grouping, B-3 with no room included. A route the package or the call records
stands.

Every number equals independent reference SQL written from the tables, never from the compiled
SQL, and every refusal that isn't such a call stays the call's own.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import binding
from semantic_rails.config import load_package_config
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.query_call import read_call
from semantic_rails.registry import Registry
from semantic_rails.request_context import context_from_policy_context
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ConnectionSpec, PackageConfig, SeedSpec, SemanticPolicyConfig
from semantic_rails.visible_view import hidden_object_ids
from tests.semantic_rails.hidden_absent import (
    CALLER,
    absent,
    envelope,
    outcome,
    visibility_policy,
    with_policies,
)

from .conftest import _rows, _runtime, _strict

CAMPUS = Path(__file__).resolve().parent / "campus"
SEED = (CAMPUS / "data" / "seed.sql").read_text(encoding="utf-8")

ROOM, BUILDING, FLOOR = "entity.campus_room", "entity.campus_building", "entity.campus_floor"
AREA = "measure.campus.assignable_area"
BUILDINGS = "measure.campus.building_count"
BUILDING_ROWS, AREA_OR_ZERO = "measure.campus.building_rows", "measure.campus.area_or_zero"
ROOM_AREA = "measure.campus.room_area"
ROOM_CODE, ROOM_USE = "dimension.campus_room_room_code", "dimension.campus_room_room_use"
ROW_CODE = "dimension.campus_room_rows_room_code"
BUILDING_NAME = "dimension.campus_building_building_name"
ORG_NAME = "dimension.campus_organization_org_name"
ROOM_BUILDING = "relationship.room_building"
VIA_FLOORS = ["relationship.room_floor", "relationship.floor_building"]

BY_ROOM = {"select": [AREA], "group_by": [ROOM_CODE]}
BY_USE = {
    "select": [
        AREA,
        BUILDINGS,
        {"expression": {"kind": "measure", "measure": AREA, "aggregation": "avg"}, "as": "avg"},
    ],
    "group_by": [ROOM_USE],
}
OFFICES = {"field": ROOM_USE, "op": "is", "value": "office"}

# Written from the tables: a room's building is the one its building_id names, and a group
# holds each building its rooms name once.
REFERENCE = {
    "by_room": """SELECT r.room_code, b.assignable_area FROM rooms r
      LEFT JOIN buildings b ON b.building_id = r.building_id""",
    "by_room_via_floors": """SELECT r.room_code, b.assignable_area FROM rooms r
      LEFT JOIN floors f ON f.floor_id = r.floor_id
      LEFT JOIN buildings b ON b.building_id = f.building_id""",
    "rooms_of_buildings_via_floors": """SELECT r.room_code, SUM(b.assignable_area)
      FROM buildings b JOIN floors f ON f.building_id = b.building_id
      JOIN rooms r ON r.floor_id = f.floor_id GROUP BY r.room_code""",
    "by_use": """SELECT g.room_use, SUM(b.assignable_area), NULLIF(COUNT(b.building_id), 0),
      AVG(b.assignable_area)
      FROM (SELECT DISTINCT room_use, building_id FROM rooms) g
      LEFT JOIN buildings b ON b.building_id = g.building_id GROUP BY g.room_use""",
    "by_row_code": """SELECT g.room_code, SUM(b.assignable_area)
      FROM (SELECT DISTINCT room_code, building_id FROM room_rows) g
      LEFT JOIN buildings b ON b.building_id = g.building_id GROUP BY g.room_code""",
    "room_and_building_area": """SELECT r.room_code, b.building_name, r.area, b.assignable_area
      FROM rooms r LEFT JOIN buildings b ON b.building_id = r.building_id""",
    "b1_rooms": """SELECT r.room_code, b.assignable_area FROM rooms r
      JOIN buildings b ON b.building_id = r.building_id WHERE b.building_name = 'B-1'""",
    # A use's buildings are those one of its rooms names, each once.
    "rows_by_use": """SELECT u.room_use,
      (SELECT NULLIF(COUNT(*), 0) FROM buildings b WHERE EXISTS (SELECT 1 FROM rooms r
        WHERE r.building_id = b.building_id AND r.room_use = u.room_use)),
      (SELECT AVG(COALESCE(b.assignable_area, 0)) FROM buildings b WHERE EXISTS (SELECT 1
        FROM rooms r WHERE r.building_id = b.building_id AND r.room_use = u.room_use))
      FROM (SELECT DISTINCT room_use FROM rooms) u""",
    # An organization's buildings are those one of its rooms names, each once.
    "by_organization": """SELECT o.org_name, SUM(b.assignable_area) FROM organizations o
      JOIN buildings b ON EXISTS (SELECT 1 FROM rooms r WHERE r.org_id = o.org_id
        AND r.building_id = b.building_id) GROUP BY o.org_name""",
    "total": "SELECT SUM(assignable_area) FROM buildings",
    "office_total": """SELECT SUM(b.assignable_area) FROM buildings b WHERE EXISTS (
      SELECT 1 FROM rooms r WHERE r.building_id = b.building_id AND r.room_use = 'office')""",
    # A North caller sees the North buildings and the rooms that name one.
    "north_rooms": """SELECT r.room_code, b.assignable_area FROM rooms r
      JOIN buildings b ON b.building_id = r.building_id WHERE b.campus = 'North'""",
    "north_total": "SELECT SUM(assignable_area) FROM buildings WHERE campus = 'North'",
    # An office caller sees the office rooms, and every building (shared).
    "office_rooms": """SELECT r.room_code, b.assignable_area FROM rooms r
      LEFT JOIN buildings b ON b.building_id = r.building_id WHERE r.room_use = 'office'""",
}


def _edit(package: Path, name: str, change: Callable[[dict[str, Any]], None]) -> None:
    path = package / name
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(spec)
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")


def _two_way(spec: dict[str, Any]) -> None:
    spec["defaults"]["relationship"]["traversal"] = ["forward", "reverse"]


def _recorded(source: str, target: str, path: list[str]) -> Callable[[dict[str, Any]], None]:
    def change(spec: dict[str, Any]) -> None:
        spec["graph"]["path_preferences"] = [
            {"source_entity": source, "target_entity": target, "relationship_path": path}
        ]

    return change


# Each variant's edits, applied in order to a copy of ``campus/``.
VARIANTS: dict[str, list[tuple[str, Callable[[dict[str, Any]], None]]]] = {
    "base": [],
    "two_way": [("package.yml", _two_way)],
    # A room's building is the one its floor names.
    "floor_route": [("graph.yml", _recorded("room", "building", VIA_FLOORS))],
    # A building's rooms are the rooms on its floors.
    "rooms_route": [
        ("package.yml", _two_way),
        (
            "graph.yml",
            _recorded(
                "building", "room", ["relationship.floor_building", "relationship.room_floor"]
            ),
        ),
    ],
    # A room's addresses are its own building's (``test_child_values``).
    "address_route": [
        ("package.yml", _two_way),
        (
            "graph.yml",
            _recorded("room", "address", [ROOM_BUILDING, "relationship.address_building"]),
        ),
    ],
}


@pytest.fixture(scope="module")
def packages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    made = {}
    for name, edits in VARIANTS.items():
        package = tmp_path_factory.mktemp(f"campus_{name}") / "campus"
        shutil.copytree(CAMPUS, package)
        for edit in edits:
            _edit(package, *edit)
        made[name] = package
    return made


class Campus:
    def __init__(
        self, runtimes: dict[str, Runtime], packages: dict[str, Path], on: dict[str, Any]
    ) -> None:
        self.runtimes = runtimes
        self.packages = packages
        self.on = on  # the package overrides that put a config on this backend

    def reference(self, name: str) -> list[tuple[Any, ...]]:
        return self.sql(REFERENCE[name])

    def sql(self, sql: str) -> list[tuple[Any, ...]]:
        """Reference SQL's rows on this backend's data, sorted."""
        return sorted(_rows(self.runtimes["base"], sql), key=repr)

    def query(self, call: dict[str, Any], variant: str = "base", **kwargs: Any) -> dict[str, Any]:
        return self.runtimes[variant].query(call, **kwargs)

    def answer(self, call: dict[str, Any], variant: str = "base") -> list[tuple[Any, ...]]:
        return _values(self.query(call, variant))

    def config(self, variant: str = "base") -> PackageConfig:
        return load_package_config(str(self.packages[variant]))

    def runtime(self, config: PackageConfig, variant: str = "base") -> Runtime:
        """A runtime of ``config`` on this backend's data."""
        if self.on:
            config = replace(config, package=replace(config.package, **self.on))
        return Runtime.from_config(config, source_path=str(self.packages[variant]))


def _values(out: dict[str, Any]) -> list[tuple[Any, ...]]:
    return sorted((tuple(row.values()) for row in out["rows"]), key=repr)


def _exact(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """``rows`` with their last column, an average, read as an exact number: Postgres answers an
    average's NUMERIC exactly, as a string."""
    return [(*row[:-1], None if row[-1] is None else Decimal(str(row[-1]))) for row in rows]


@pytest.fixture(scope="module", params=["duckdb", "postgres"])
def campus(request: pytest.FixtureRequest, packages: dict[str, Path]) -> Iterator[Campus]:
    if request.param == "duckdb":
        runtimes = {name: _runtime(path) for name, path in packages.items()}
        try:
            for runtime in runtimes.values():
                _rows(runtime, "SELECT 1")  # the fixture owns each connection
            yield Campus(runtimes, packages, {})
        finally:
            for runtime in runtimes.values():
                runtime.close()
        return
    required = ("SR_POSTGRES_HOST", "SR_POSTGRES_USER", "SR_POSTGRES_PASSWORD")
    if any(not os.environ.get(name, "").strip() for name in required):
        pytest.skip("postgres: SR_POSTGRES_* unset (run through with_postgres.sh)")
    schema = f"sr_campus_{uuid.uuid4().hex[:12]}"
    options = {
        "host_env": "SR_POSTGRES_HOST",
        "port": os.environ.get("SR_POSTGRES_PORT", "5433"),
        "database": os.environ.get("SR_POSTGRES_DATABASE", "sr_jaffle"),
        "user_env": "SR_POSTGRES_USER",
        "password_env": "SR_POSTGRES_PASSWORD",
    }

    def on(schema_options: dict[str, str]) -> dict[str, Any]:
        connection = ConnectionSpec(kind="postgres_native", name="", options=schema_options)
        return {
            "warehouse": "postgres",
            "default_db": "",
            "seed": SeedSpec(),
            "connection": connection,
        }

    admin = _runtime(packages["base"], **on(options))
    try:
        _rows(admin, f"CREATE SCHEMA {schema}")
    except Exception as exc:
        admin.close()
        if _strict():
            raise
        pytest.skip(f"postgres: unreachable ({type(exc).__name__})")
    overrides = on({**options, "schema": schema})
    runtimes: dict[str, Runtime] = {}
    try:
        for name, path in packages.items():
            runtimes[name] = _runtime(path, **overrides)
        _rows(runtimes["base"], SEED)
        yield Campus(runtimes, packages, overrides)
    finally:
        for runtime in runtimes.values():
            runtime.close()
        try:
            _rows(admin, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()


def _parent_values(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        warning
        for warning in out.get("warnings", [])
        if warning["code"] == "REWRITE_APPLIED"
        and warning["details"].get("rewrite_kind") == "parent_value"
    ]


def _note(route_reading: str, population: str = ", including those with no Room rows") -> str:
    return (
        f"Read on each Room row from {route_reading}: each Building counts once, with its whole "
        "value, in every group that holds one of its Room rows, so the column repeats on each of "
        "its Room rows and doesn't add up across groups, and a group whose Room rows have no "
        "Building reads NULL. A total (totals, or a total item) is the call without the "
        f"grouping: it counts each Building once{population}."
    )


@contextmanager
def _without_parent_values() -> Iterator[None]:
    """Calls bound as their own bind binds them: its answer, or its refusal."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(binding, "bind_parent_values", lambda *args, **kwargs: None)
        yield


def _refusal(call: Callable[[], Any]) -> tuple[str, str, dict[str, Any]]:
    with pytest.raises(SemanticLayerError) as refused:
        call()
    return refused.value.code, str(refused.value), refused.value.details


@pytest.mark.parametrize("variant", ["base", "two_way"])
@pytest.mark.parametrize("verbosity", ["minimal", "full"])
def test_each_room_row_reads_its_own_buildings_value(campus, variant, verbosity):
    """R-101, R-102 and R-104 each show all of B-1's 1000; R-103 names no building: NULL."""
    with _without_parent_values(), pytest.raises(SemanticLayerError) as refused:
        campus.query(BY_ROOM, variant)
    assert refused.value.code == {"base": "PATH_NOT_FOUND", "two_way": "AMBIGUOUS_PATH"}[variant]
    out = campus.query(BY_ROOM, variant, verbosity=verbosity)
    assert _values(out) == campus.reference("by_room")
    assert ("R-103", None) in _values(out)
    (note,) = _parent_values(out)
    assert note["message"] == _note("the Room's Building")
    assert note["object_ids"] == [AREA]
    assert note["details"] == {
        "rows_entity": ROOM,
        "path": [ROOM_BUILDING],
        "group_by": [ROOM_CODE],
        "rewrite_kind": "parent_value",
    }
    response = SemanticLayerMCPAdapter(campus.runtimes[variant]).call_tool(
        "execute", {"query": BY_ROOM, "mode": "run", "verbosity": verbosity}
    )
    assert _note("the Room's Building") in str(response)


@pytest.mark.parametrize("variant", ["base", "two_way"])
def test_a_group_counts_each_of_its_buildings_once(campus, variant):
    """Labs R-101 and R-104 are both in B-1: 1000, not 2000. Offices span B-1 and B-2: 1600.
    Storage's one room names no building: NULL area, count and average, never 0."""
    assert _exact(campus.answer(BY_USE, variant)) == _exact(campus.reference("by_use"))
    rows = {row[0]: row[1:] for row in _exact(campus.answer(BY_USE, variant))}
    assert rows["lab"] == (1000, 1, 1000) and rows["office"] == (1600, 2, 800)
    assert rows["storage"] == (None, None, None)


@pytest.mark.parametrize("variant", ["base", "two_way"])
def test_a_room_with_no_building_adds_no_building(campus, variant):
    """How many buildings have offices? R-102 and R-201 name B-1 and B-2, and R-105 names no
    building: a count of 1 per building row reads 2, not 3, and the average of the area read as
    0 where it's missing reads 800, not 533, whatever the measure's expression reads where no
    building is there."""
    average = {"kind": "measure", "measure": AREA_OR_ZERO, "aggregation": "avg"}
    call = {"select": [BUILDING_ROWS, {"expression": average, "as": "avg"}], "group_by": [ROOM_USE]}
    answer = _exact(campus.answer(call, variant))
    assert answer == _exact(campus.reference("rows_by_use"))
    rows = {row[0]: row[1:] for row in answer}
    assert rows["office"] == (2, 800) and rows["storage"] == (None, None)


def test_rows_without_a_key_count_each_building_once(campus):
    """room_rows stores R-101 twice: its building's area is still 1000, not 2000."""
    call = {"select": [AREA], "group_by": [ROW_CODE]}
    assert campus.answer(call) == campus.reference("by_row_code")
    assert ("R-101", 1000) in campus.answer(call)


def test_a_rooms_own_measure_and_its_buildings_read_side_by_side(campus):
    call = {"select": [ROOM_AREA, AREA], "group_by": [ROOM_CODE, BUILDING_NAME]}
    assert campus.answer(call) == campus.reference("room_and_building_area")
    where = {"select": [AREA], "group_by": [ROOM_CODE]}
    where["where"] = [{"field": BUILDING_NAME, "op": "is", "value": "B-1"}]
    assert campus.answer(where) == campus.reference("b1_rooms")


@pytest.mark.parametrize("variant", ["base", "two_way"])
def test_a_total_counts_every_building_once(campus, variant):
    """The total is the call without the grouping: all three buildings, B-3 with no room
    included; with a condition on the rooms, the buildings with such a room."""
    (total,) = campus.reference("total")
    out = campus.query({**BY_ROOM, "totals": []}, variant)
    assert _values(out) == sorted(
        [(*row, None) for row in campus.reference("by_room")] + [(None, *total, ROOM_CODE)],
        key=repr,
    )
    assert [note["message"] for note in _parent_values(out)] == [_note("the Room's Building")]
    item = {"expression": {"kind": "total", "input": AREA, "over": [ROOM_CODE]}, "as": "all"}
    whole = campus.answer({**BY_ROOM, "select": [AREA, item]}, variant)
    assert whole == sorted((*row, *total) for row in campus.reference("by_room"))
    (office_total,) = campus.reference("office_total")
    offices = campus.query({**BY_ROOM, "where": [OFFICES], "totals": []}, variant)
    assert (None, *office_total, ROOM_CODE) in _values(offices)
    assert [note["message"] for note in _parent_values(offices)] == [
        _note("the Room's Building", population="")
    ]


CALLS = {
    "by_room": BY_ROOM,
    "by_use": BY_USE,
    "by_row_code": {"select": [AREA], "group_by": [ROW_CODE]},
    "totals": {**BY_ROOM, "totals": []},
    "offices": {**BY_ROOM, "where": [OFFICES]},
}


@pytest.mark.parametrize("name", CALLS)
def test_forward_only_and_two_way_packages_answer_alike(campus, name):
    """A two-way package's routes from a building to its rooms are a question; the rooms' own
    key asks none, so both packages read the same rows and say the same."""
    forward, two_way = (campus.query(CALLS[name], variant) for variant in ("base", "two_way"))
    assert _values(forward) == _values(two_way)
    assert forward["warnings"] == two_way["warnings"]


def test_a_recorded_route_from_the_rooms_is_the_one_read(campus):
    """Recorded, a room's building is its floor's: R-202 names B-2 but sits on a floor of B-1."""
    out = campus.query(BY_ROOM, "floor_route")
    assert _values(out) == campus.reference("by_room_via_floors")
    assert ("R-202", 1000) in _values(out)
    (note,) = _parent_values(out)
    assert note["details"]["path"] == VIA_FLOORS
    # The call's own route reads the same.
    route = {"from": ROOM, "to": BUILDING, "via": VIA_FLOORS}
    assert campus.answer({**BY_ROOM, "routes": [route]}) == campus.reference("by_room_via_floors")


def test_a_recorded_route_from_the_buildings_stands(campus):
    """Recorded, a building's rooms are the rooms on its floors: the package's route answers,
    from the buildings' side, as it does without parent values (R-103, on no floor, drops out)."""
    out = campus.query(BY_ROOM, "rooms_route")
    assert _values(out) == campus.reference("rooms_of_buildings_via_floors")
    assert not _parent_values(out)
    with _without_parent_values():
        assert campus.answer(BY_ROOM, "rooms_route") == _values(out)
    # A call the package's route doesn't answer keeps its own refusal: read by each room's own
    # key, R-202 would show B-2's 600 where the recorded route reads B-1's 1000.
    by_organization = {**BY_ROOM, "group_by": [ROOM_CODE, ORG_NAME]}
    refused = _refusal(lambda: campus.query(by_organization, "rooms_route"))
    assert refused[0] == "AMBIGUOUS_PATH"
    with _without_parent_values():
        assert refused == _refusal(lambda: campus.query(by_organization, "rooms_route"))


def test_the_value_reads_as_fresh_as_the_buildings_model(campus):
    """The leaf reads the rooms' rows and the buildings' values: each model's freshness is
    stated."""
    config = campus.config()
    stamps = {ROOM: "2026-10-02T00:00:00Z", BUILDING: "2026-10-01T00:00:00Z"}
    config = replace(
        config,
        entities=[
            replace(row, freshness_as_of=stamps[row.id]) if row.id in stamps else row
            for row in config.entities
        ],
    )
    runtime = campus.runtime(config)
    try:
        out = runtime.query(BY_ROOM)
    finally:
        runtime.close()
    assert sorted((row["source_entity"], row["as_of"]) for row in out["freshness_by_leaf"]) == [
        (BUILDING, stamps[BUILDING]),
        (ROOM, stamps[ROOM]),
    ]
    assert out["freshness_as_of"] == stamps[BUILDING]


def _row_filter(name: str, dimension: str, attribute: str, **config: Any) -> SemanticPolicyConfig:
    return SemanticPolicyConfig(
        id=f"policy.campus.{name}",
        kind="row_filter",
        roles=["facilities"],
        config={"dimension": dimension, "attribute": attribute, **config},
    )


CAMPUS_FILTER = _row_filter("campus", "dimension.campus_building_campus", "campus")
USE_FILTER = _row_filter("use", ROOM_USE, "use")
SHARED_USE_FILTER = _row_filter("use", ROOM_USE, "use", shared=[BUILDING])
NORTH = RequestContext(roles=("facilities",), attributes={"campus": "North"})
OFFICE_STAFF = RequestContext(roles=("facilities",), attributes={"use": "office"})


@pytest.mark.parametrize("variant", ["base", "two_way"])
def test_a_row_filter_on_buildings_reads_the_visible_rooms_only(campus, variant):
    """A North caller sees B-1 and B-3, and the rooms that name one: R-103 names none."""
    runtime = campus.runtime(with_policies(campus.config(variant), CAMPUS_FILTER), variant)
    try:
        out = runtime.query({**BY_ROOM, "totals": []}, context=NORTH)
        (total,) = campus.reference("north_total")
        assert _values(out) == sorted(
            [(*row, None) for row in campus.reference("north_rooms")] + [(None, *total, ROOM_CODE)],
            key=repr,
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("variant", ["base", "two_way"])
def test_a_row_filter_on_rooms_reads_buildings_only_where_they_are_shared(campus, variant):
    """An office caller sees R-102, R-201 and R-105. With every building shared, each office
    reads its building's value (R-105, naming none, NULL); otherwise no filter reaches the buildings, so their measure is refused as
    it is asked alone."""
    shared = campus.runtime(with_policies(campus.config(variant), SHARED_USE_FILTER), variant)
    held = campus.runtime(with_policies(campus.config(variant), USE_FILTER), variant)
    try:
        out = shared.query(BY_ROOM, context=OFFICE_STAFF)
        assert _values(out) == campus.reference("office_rooms")
        refused = _refusal(lambda: held.query(BY_ROOM, context=OFFICE_STAFF))
        assert refused[0] == "POLICY_DENIED"
        assert refused[2]["reason"] == "row_filter_uncovered_root"
        alone = _refusal(lambda: held.query({"select": [AREA]}, context=OFFICE_STAFF))
        assert refused == alone
    finally:
        shared.close()
        held.close()


def _execute(campus: Campus, config: PackageConfig, variant: str, call: dict[str, Any]) -> Any:
    runtime = campus.runtime(config, variant)
    try:
        return outcome(
            lambda: envelope(
                SemanticLayerMCPAdapter(runtime).call_tool(
                    "execute",
                    {"query": call, "verbosity": "full"},
                    request_context=context_from_policy_context(CALLER),
                )
            )
        )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("variant", "hidden_id"), [("base", BUILDING), ("two_way", FLOOR)], ids=["buildings", "floors"]
)
def test_a_hidden_object_reads_as_a_package_without_it(campus, variant, hidden_id):
    """A caller who can't see buildings gets what a package without buildings answers. One who
    can't see floors on a two-way package (where a building reaches its rooms through floors
    too) gets what a package without floors answers: each room's own building."""
    config = with_policies(campus.config(variant), visibility_policy("hidden", hidden_id))
    hidden = hidden_object_ids(config, roles=CALLER["roles"])
    assert hidden_id in hidden
    call = {**BY_ROOM, "order_by": [{"field": ROOM_CODE}]}
    response = _execute(campus, config, variant, call)
    assert response == _execute(campus, absent(config, hidden), variant, call)
    if hidden_id == FLOOR:
        rows = response["rows"]
        assert sorted((row[ROOM_CODE], row["assignable_area"]) for row in rows) == sorted(
            campus.reference("by_room"), key=repr
        )


MEDIAN = {"kind": "measure", "measure": AREA, "aggregation": "median"}
REFUSED = {
    # Not a whole-row aggregation: a median of the buildings per group is a later question.
    "median": {"select": [{"expression": MEDIAN, "as": "median"}], "group_by": [ROOM_CODE]},
    # A ratio of a room's measure to its building's stays refused.
    "ratio": {
        "select": [
            {
                "expression": {"kind": "ratio", "numerator": ROOM_AREA, "denominator": AREA},
                "as": "share",
            }
        ],
        "group_by": [ROOM_CODE],
    },
    # An organization's buildings are reached through its rooms, one to many.
    "by_organization": {"select": [AREA], "group_by": [ORG_NAME]},
    # A maximum isn't additive.
    "peak_occupancy": {"select": ["measure.campus.peak_occupancy"], "group_by": [ROOM_CODE]},
}


@pytest.mark.parametrize("variant", ["base", "two_way"])
@pytest.mark.parametrize("name", REFUSED)
def test_any_other_call_keeps_its_own_refusal(campus, variant, name):
    refused = _refusal(lambda: campus.query(REFUSED[name], variant))
    with _without_parent_values():
        assert refused == _refusal(lambda: campus.query(REFUSED[name], variant))
    assert refused[0] in {"PATH_NOT_FOUND", "AMBIGUOUS_PATH", "INVALID_QUERY"}


def test_buildings_by_organization_is_a_route_question(campus):
    """Each organization's building area: which link ties a building to an organization is a
    business route, so the rooms don't decide it. A two-way package asks, and each option's
    call answers from the buildings' side: each building counts once, with its whole area, in
    every organization with one of its rooms."""
    code, _, details = _refusal(lambda: campus.query(REFUSED["by_organization"], "two_way"))
    assert code == "AMBIGUOUS_PATH"
    (rooms,) = [
        option
        for option in details["clarification"]["options"]
        if option["relationship_path"] == [ROOM_BUILDING, "relationship.room_organization"]
    ]
    assert campus.answer(rooms["query"], "two_way") == campus.reference("by_organization")


def _on_clickhouse(config: PackageConfig) -> PackageConfig:
    connection = ConnectionSpec(
        kind="clickhouse_native",
        options={"host_env": "SR_CLICKHOUSE_HOST", "user_env": "SR_CLICKHOUSE_USER"},
    )
    return replace(
        config,
        package=replace(
            config.package,
            warehouse="clickhouse",
            default_db="",
            seed=SeedSpec(),
            connection=connection,
        ),
    )


@pytest.mark.parametrize("variant", ["base", "two_way"])
@pytest.mark.parametrize("name", ["by_room", "by_use"])
def test_clickhouse_keeps_the_refusal(packages, variant, name):
    """ClickHouse joins lookups INNER (its outer join reads a type default for an unmatched
    row), so a room with no building would drop out there instead of reading NULL: the call
    keeps its own refusal."""
    config = _on_clickhouse(load_package_config(str(packages[variant])))
    registry = Registry(config)

    def compiled() -> Any:
        return compile_query(config, registry, read_call(CALLS[name], config=config)[0])

    refused = _refusal(compiled)
    with _without_parent_values():
        assert refused == _refusal(compiled)
    assert refused[0] == {"base": "PATH_NOT_FOUND", "two_way": "AMBIGUOUS_PATH"}[variant]
