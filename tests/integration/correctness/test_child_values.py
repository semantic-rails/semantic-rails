"""A grouping by a child's values after a lookup, on DuckDB and Postgres.

Fixture ``campus/`` (``test_parent_values``): a room names its building, and an address names
its building. B-1 has three addresses, two in Springfield and one in Shelbyville; B-2 has none;
B-3, with no room, has one in Shelbyville. "Room area by city" groups the rooms by the city of
their building's addresses: a lookup, then a one-to-many hop. Where the route is decided and
the package walks the hop from the building, each room row counts once, with its whole value,
in every city its building has an address in (B-1's two Springfield addresses count once), so
the cities don't add up to the total and the answer says so; a room whose building has no
address (R-201, R-202) or that names no building (R-103, R-105) is the NULL city; and a total
is the call without the grouping. A package that walks forward only keeps its refusal, an
undecided route its question, and ClickHouse (which joins lookups inner) the call's own.

Every number equals independent reference SQL written from the tables, never from the compiled
SQL, and every refusal that isn't such a call stays the call's own.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts import leaf_paths
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call
from semantic_rails.registry import Registry
from semantic_rails.visible_view import hidden_object_ids
from tests.semantic_rails.hidden_absent import absent, visibility_policy, with_policies

from . import test_parent_values
from .test_parent_values import (
    AREA,
    CALLER,
    CAMPUS_FILTER,
    FLOOR,
    NORTH,
    ORG_NAME,
    ROOM,
    ROOM_AREA,
    ROOM_BUILDING,
    ROOM_USE,
    VIA_FLOORS,
    Campus,
    _execute,
    _on_clickhouse,
    _refusal,
    _values,
)

# The campus fixture, each backend's runtimes of each variant (``test_parent_values``).
campus, packages = test_parent_values.campus, test_parent_values.packages

ADDRESS = "entity.campus_address"
CITY, STREET = "dimension.campus_address_city", "dimension.campus_address_street"
ROW_AREA = "measure.campus.row_area"
ROOMS = "measure.campus.room_count"
ADDRESS_BUILDING = "relationship.address_building"
VIA_BUILDING = [ROOM_BUILDING, ADDRESS_BUILDING]
BY_BUILDING = [{"from": ROOM, "to": ADDRESS, "via": VIA_BUILDING}]
BY_CITY = {"select": [ROOM_AREA, ROOMS], "group_by": [CITY], "routes": BY_BUILDING}


def _by_city(rows: str) -> str:
    """Area and count per city: the rows (``rows``: area, building_id) whose building has an
    address in the city, then the rows whose building has none. A city only buildings with no
    rows have isn't listed."""
    has = "SELECT 1 FROM addresses a WHERE a.building_id = g.building_id"
    return f"""WITH g AS ({rows})
      SELECT c.city,
        (SELECT SUM(g.area) FROM g WHERE EXISTS ({has} AND a.city = c.city)),
        (SELECT COUNT(*) FROM g WHERE EXISTS ({has} AND a.city = c.city))
      FROM (SELECT DISTINCT a.city FROM addresses a JOIN g ON a.building_id = g.building_id) c
      UNION ALL
      SELECT NULL, SUM(g.area), COUNT(*) FROM g WHERE NOT EXISTS ({has})"""


REFERENCE = {
    "by_city": _by_city("SELECT area, building_id FROM rooms"),
    # A room's building through its floor: R-202 sits on a floor of B-1.
    "by_city_via_floors": _by_city(
        "SELECT r.area, f.building_id FROM rooms r LEFT JOIN floors f ON f.floor_id = r.floor_id"
    ),
    # Every stored row, R-101's two included.
    "rows_by_city": _by_city("SELECT area, building_id FROM room_rows"),
    # A North caller sees B-1 and B-3, and the rooms that name one.
    "north_by_city": _by_city(
        "SELECT r.area, r.building_id FROM rooms r JOIN buildings b "
        "ON b.building_id = r.building_id WHERE b.campus = 'North'"
    ),
    "by_org_city": """SELECT o.org_name, c.city, SUM(r.area) FROM rooms r
      LEFT JOIN organizations o ON o.org_id = r.org_id
      LEFT JOIN (SELECT building_id, city FROM addresses GROUP BY building_id, city) c
        ON c.building_id = r.building_id
      GROUP BY o.org_name, c.city""",
    "by_org": """SELECT o.org_name, SUM(r.area) FROM rooms r
      LEFT JOIN organizations o ON o.org_id = r.org_id GROUP BY o.org_name""",
    "total": "SELECT SUM(area), COUNT(*) FROM rooms",
}


def _reference(campus: Campus, name: str) -> list[tuple[Any, ...]]:
    """The reference's rows, less a NULL city no row reads (its count is 0)."""
    return [row for row in campus.sql(REFERENCE[name]) if row[-1] != 0]


def _notes(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        warning
        for warning in out.get("warnings", [])
        if warning["code"] == "REWRITE_APPLIED"
        and warning["details"].get("rewrite_kind") == "child_values"
    ]


NOTE = (
    "Grouped by 'dimension.campus_address_city' from any of the Addresses of the Room's "
    "Building: each Room row counts once, with its whole value, in every group of values its "
    "Building's Address rows hold (Address rows with the same values count once), so the groups "
    "overlap and don't add up to the total, and a Room row whose Building has no Address row, or "
    "that names no Building, is the NULL group. A total (totals, or a total item) is the call "
    "without the grouping: it counts each Room row once."
)


@contextmanager
def _without_child_values() -> Iterator[None]:
    """Calls bound as before: the leaf's own answer, or its refusal."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(leaf_paths, "child_values_route", lambda *args, **kwargs: None)
        yield


@pytest.mark.parametrize("variant", ["two_way", "address_route"])
def test_a_room_counts_once_in_every_city_its_building_has_an_address_in(campus, variant):
    """B-1's rooms (65 of area, 3 rooms) count in Springfield and in Shelbyville, and B-1's two
    Springfield addresses count once: not 130. B-2's rooms, R-103 and R-105 are the NULL city.
    The route is the call's own, or the package's recorded one."""
    call = BY_CITY if variant == "two_way" else {**BY_CITY, "routes": []}
    with _without_child_values(), pytest.raises(SemanticLayerError) as refused:
        campus.query(call, variant)
    assert refused.value.code == "MIXED_GRAIN_INVALID"
    out = campus.query(call, variant)
    assert _values(out) == _reference(campus, "by_city")
    rows = {row[0]: row[1:] for row in _values(out)}
    assert rows == {"Springfield": (65, 3), "Shelbyville": (65, 3), None: (125, 4)}
    notes = _notes(out)
    assert [note["object_ids"] for note in notes] == [[ROOM_AREA], [ROOMS]]
    assert notes[0]["message"] == NOTE
    assert notes[0]["details"] == {
        "path": VIA_BUILDING,
        "overlapping_groups": {
            "group_by": [CITY],
            "relationships": [ADDRESS_BUILDING],
            "whole_amounts": True,
        },
        "rewrite_kind": "child_values",
    }
    # A count is no amount to split over the addresses.
    assert "whole_amounts" not in notes[1]["details"]["overlapping_groups"]


def test_the_cities_dont_add_up_to_the_total(campus):
    """Springfield, Shelbyville and the NULL city hold 255 of area between them; the total, the
    call without the grouping, is every room once: 190."""
    out = campus.query({**BY_CITY, "totals": []}, "two_way")
    groups = [row for row in out["rows"] if row["total_over"] is None]
    (total,) = [row for row in out["rows"] if row["total_over"] is not None]
    assert sum(row["room_area"] for row in groups) == 255
    assert [(total["room_area"], total["room_count"])] == campus.sql(REFERENCE["total"])
    assert "don't add up to the total" in _notes(out)[0]["message"]


def test_rows_without_a_key_each_count_once_per_city(campus):
    """room_rows stores R-101 twice: each stored row counts, in each of B-1's cities."""
    via = ["relationship.room_rows_building", ADDRESS_BUILDING]
    routes = [{"from": "entity.campus_room_rows", "to": ADDRESS, "via": via}]
    out = campus.query({"select": [ROW_AREA], "group_by": [CITY], "routes": routes}, "two_way")
    assert _values(out) == [row[:2] for row in _reference(campus, "rows_by_city")]
    assert ("Springfield", 95) in _values(out)
    assert "each row of Room rows counts once" in _notes(out)[0]["message"]


def test_the_route_the_call_gives_is_the_one_read(campus):
    """Through its floor, R-202's building is B-1: it counts in B-1's cities, not the NULL one."""
    routes = [{"from": ROOM, "to": ADDRESS, "via": [*VIA_FLOORS, ADDRESS_BUILDING]}]
    out = campus.query({**BY_CITY, "routes": routes}, "two_way")
    assert _values(out) == _reference(campus, "by_city_via_floors")
    assert ("Springfield", 105, 4) in _values(out)
    assert _notes(out)[0]["details"]["path"] == [*VIA_FLOORS, ADDRESS_BUILDING]


def test_another_grouping_and_its_subtotals_read_the_rooms_as_ever(campus):
    """By organization and city, with a subtotal per organization: each subtotal is the call
    grouped by organization alone, every room once."""
    call = {"select": [ROOM_AREA], "group_by": [ORG_NAME, CITY], "routes": BY_BUILDING}
    out = campus.query({**call, "totals": [ORG_NAME]}, "two_way")
    rows = out["rows"]
    own = [(row[ORG_NAME], row[CITY], row["room_area"]) for row in rows if not row["total_over"]]
    assert sorted(own, key=repr) == campus.sql(REFERENCE["by_org_city"])
    subtotals = [(row[ORG_NAME], row["room_area"]) for row in rows if row["total_over"] == CITY]
    assert sorted(subtotals, key=repr) == campus.sql(REFERENCE["by_org"])


def test_two_columns_of_the_child_are_the_values_one_address_holds(campus):
    call = {"select": [ROOM_AREA], "group_by": [CITY, STREET], "routes": BY_BUILDING}
    assert campus.answer(call, "two_way") == sorted(
        [
            ("Shelbyville", "9 Elm St", 65),
            ("Springfield", "1 Main St", 65),
            ("Springfield", "3 Main St", 65),
            (None, None, 125),
        ],
        key=repr,
    )


def test_a_filter_on_the_rooms_reads_their_rows_only(campus):
    call = {**BY_CITY, "where": [{"field": ROOM_USE, "op": "is", "value": "lab"}]}
    rows = {row[0]: row[1:] for row in campus.answer(call, "two_way")}
    assert rows == {"Springfield": (45, 2), "Shelbyville": (45, 2)}


def test_a_row_filter_on_buildings_reads_the_visible_rooms_only(campus):
    """A North caller sees B-1's rooms: no room of B-2, and not R-103, which names no
    building. Under a row filter the call gives no route: the package's recorded one reads."""
    config = with_policies(campus.config("address_route"), CAMPUS_FILTER)
    runtime = campus.runtime(config, "address_route")
    try:
        out = runtime.query({**BY_CITY, "routes": []}, context=NORTH)
    finally:
        runtime.close()
    assert _values(out) == _reference(campus, "north_by_city")
    assert None not in {row[0] for row in _values(out)}


@pytest.mark.parametrize("hidden_id", [FLOOR, ADDRESS], ids=["floors", "addresses"])
def test_a_hidden_object_reads_as_a_package_without_it(campus, hidden_id):
    """A caller who can't see floors gets the rooms by city, as a package without floors
    answers; one who can't see addresses has no city to group by, as without them."""
    config = with_policies(campus.config("address_route"), visibility_policy("hidden", hidden_id))
    hidden = hidden_object_ids(config, roles=CALLER["roles"])
    assert hidden_id in hidden
    call = {**BY_CITY, "routes": [], "order_by": [{"field": ROOM_AREA}]}
    response = _execute(campus, config, "address_route", call)
    assert response == _execute(campus, absent(config, hidden), "address_route", call)
    if hidden_id == FLOOR:
        rows = response["rows"]
        assert sorted(
            ((row[CITY], row["room_area"], row["room_count"]) for row in rows), key=repr
        ) == _reference(campus, "by_city")


MEDIAN = {"kind": "measure", "measure": ROOM_AREA, "aggregation": "median"}
SHARE = {"kind": "share", "input": ROOM_AREA, "over": [CITY]}
RATIO = {"kind": "ratio", "numerator": ROOM_AREA, "denominator": AREA}
ROUTED = {"group_by": [CITY], "routes": BY_BUILDING}
REFUSED = {
    # The package walks an address's building only from the address: no city for a room.
    "forward_only": ("base", {"select": [ROOM_AREA], "group_by": [CITY]}),
    # Two routes reach a room's addresses (its building, its floor's building): the question.
    "undecided": ("two_way", {"select": [ROOM_AREA], "group_by": [CITY]}),
    # A filter on the child beside its grouping.
    "where_city": (
        "two_way",
        {**BY_CITY, "where": [{"field": CITY, "op": "is", "value": "Springfield"}]},
    ),
    # Not a whole-row aggregation.
    "median": ("two_way", {"select": [{"expression": MEDIAN, "as": "m"}], **ROUTED}),
    # A share over overlapping groups: its total would add them up.
    "share": ("two_way", {"select": [{"expression": SHARE, "as": "s"}], **ROUTED}),
    # A room's measure against its building's.
    "ratio": ("two_way", {"select": [{"expression": RATIO, "as": "r"}], **ROUTED}),
}
CODES = {"forward_only": "PATH_NOT_FOUND", "undecided": "AMBIGUOUS_PATH"}


@pytest.mark.parametrize("name", REFUSED)
def test_any_other_call_keeps_its_own_refusal(campus, name):
    variant, call = REFUSED[name]
    refused = _refusal(lambda: campus.query(call, variant))
    with _without_child_values():
        assert refused == _refusal(lambda: campus.query(call, variant))
    assert refused[0] == CODES.get(name, "MIXED_GRAIN_INVALID")


@pytest.mark.parametrize("variant", ["two_way", "address_route"])
def test_clickhouse_keeps_the_refusal(packages, variant):
    """ClickHouse joins lookups INNER (its outer join reads a type default for an unmatched
    row), so a room with no building, or whose building has no address, would drop out there
    instead of being the NULL city: the call keeps its own refusal."""
    config = _on_clickhouse(load_package_config(str(packages[variant])))
    registry = Registry(config)
    call = BY_CITY if variant == "two_way" else {**BY_CITY, "routes": []}

    def compiled() -> Any:
        return compile_query(config, registry, read_call(call, config=config)[0])

    refused = _refusal(compiled)
    with _without_child_values():
        assert refused == _refusal(compiled)
    assert refused[0] == "MIXED_GRAIN_INVALID"
