"""A joined call (``parts`` and ``join``): calls answered as their own, joined on their keys in
one statement, checked against SQL written by hand from the tables.

The ``depts`` package holds HR and facilities rows that both carry a department code, joined by
declared relationships to the departments model; facilities' own assignments, keyed by building
and department code, are related to nothing (LAB is a code HR doesn't have, and Finance and
Marketing have no assignment). Engineering is two people, both named Alex;
two people have no department and one is filed under UNASSIGNED, a code read as unknown;
Marketing has people and no space, Legal space and no people, and 900 sq ft is assigned to no
department. Work orders are loaded through 25 February and reservations through 15 March; room
R3 has reservations only.

The hand SQL settles a key the way the engine's rule says, written out: a part's count or sum
reads 0 on a key only another part has, its average NULL; a NULL key never joins, so each
part's NULL-key row is its own row, NULL in the other part.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.package_tools import _run_test
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ConnectionSpec, PackageConfig, SeedSpec, SemanticPolicyConfig
from tests.semantic_rails.hidden_absent import visibility_policy

from .conftest import _rows, _runtime, _strict

DEPTS = Path(__file__).resolve().parent / "depts"
SEED = (DEPTS / "data" / "seed.sql").read_text(encoding="utf-8")
NOW = "2024-03-20T00:00:00Z"

DEPT = "dimension.depts_department_dept_code"
SPACE_DEPT = "dimension.depts_space_dept_code"
EMPLOYEE_DEPT = "dimension.depts_employee_dept_code"
NAME = "dimension.depts_employee_employee_name"
EMPLOYEE = "dimension.depts_employee_id"
BUILDING = "dimension.depts_space_building_key"
DEPT_NAME = "dimension.depts_department_dept_name"
ROOM = "dimension.depts_room_room_key"
HEADCOUNT = {"select": ["measure.depts.headcount"], "group_by": [DEPT]}
SPACE = {
    "select": ["measure.depts.area", "measure.depts.buildings", "measure.depts.average_area"],
    "group_by": [SPACE_DEPT],
}
BY_DEPARTMENT = {"parts": [HEADCOUNT, SPACE], "join": {"on": [[DEPT, SPACE_DEPT]]}}
ASSIGNMENT_DEPT = "dimension.depts_assignment_dept_code"
ASSIGNED = {
    "select": [
        "measure.depts.assigned_area",
        "measure.depts.assigned_buildings",
        "measure.depts.average_assigned_area",
    ],
    "group_by": [ASSIGNMENT_DEPT],
}
# No relationship joins the assignments to HR: both key a dept_code, so the caller may join them.
BY_ASSIGNMENT = {"parts": [HEADCOUNT, ASSIGNED], "join": {"on": [[DEPT, ASSIGNMENT_DEPT]]}}
IN_BUILDING_32 = {
    "select": ["measure.depts.area"],
    "group_by": [SPACE_DEPT],
    "where": [{"field": BUILDING, "op": "is", "value": 32}],
}
WORK_ORDERS = {"select": ["measure.depts.work_orders"], "group_by": [ROOM]}
RESERVATIONS = {"select": ["measure.depts.reservations"], "group_by": [ROOM]}
MONTHS = {"grain": "month", "start": "2024-01-01", "end": "2024-04-01"}
SITE = "dimension.depts_employee_site"
# Departments with more than 2 people: an entity filter, which removes whole keys of its part.
BIG_DEPARTMENTS = {
    "entity": "entity.depts_department",
    "over": "all_time",
    "having": [{"field": "measure.depts.headcount", "op": "gt", "value": 2}],
}

CAVEAT = (
    "Each part was aggregated to its own groups before the join, so no row counts twice within "
    "a part. It does not check that the joined columns mean the same thing in both models; the "
    "join is the caller's."
)
SITE_LEAD = RequestContext(roles=("site_lead",), attributes={"site": "south"})


def _site_filter(**config: Any) -> SemanticPolicyConfig:
    return SemanticPolicyConfig(
        id="policy.depts.site",
        kind="row_filter",
        roles=["site_lead"],
        config={"dimension": "dimension.depts_employee_site", "attribute": "site", **config},
    )


# The department code HR's headcount is grouped by: the departments model's, which reads
# UNASSIGNED as unknown; the employees' own column reads it as written.
UNKNOWN_AS_NULL = "CASE WHEN dept_code = 'UNASSIGNED' THEN NULL ELSE dept_code END"


def _by_department(
    employees: str = "TRUE",
    space: str = "TRUE",
    keep: str = "",
    *,
    table: str = "space",
    code: str = UNKNOWN_AS_NULL,
) -> str:
    """Headcount beside area by department code, by hand: each side grouped by its own code
    (UNASSIGNED read as unknown), FULL OUTER JOINed on plain equality, a count or sum 0 on a
    code only the other side has, NULL on the other side's NULL-code row. ``table`` is the
    facilities side: the space rows or the assignments."""
    return f"""
WITH hr AS (
  SELECT {code} AS code, COUNT(*) AS headcount, 1 AS present
  FROM employees WHERE {employees}
  GROUP BY {code}),
fac AS (
  SELECT dept_code AS code, SUM(area_sqft) AS area, COUNT(DISTINCT building_key) AS buildings,
         AVG(area_sqft) AS average_area, 1 AS present
  FROM {table} WHERE {space} GROUP BY dept_code)
SELECT COALESCE(hr.code, fac.code),
  CASE WHEN hr.present = 1 THEN hr.headcount WHEN fac.code IS NOT NULL THEN 0 END,
  CASE WHEN fac.present = 1 THEN fac.area WHEN hr.code IS NOT NULL THEN 0 END,
  CASE WHEN fac.present = 1 THEN fac.buildings WHEN hr.code IS NOT NULL THEN 0 END,
  fac.average_area
FROM hr FULL OUTER JOIN fac ON hr.code = fac.code
{f"WHERE {keep}" if keep else ""}"""


def _department_counts(
    employees: str = "TRUE", space: str = "TRUE", *, table: str = "space"
) -> str:
    """Per side: its rows, its NULL-code rows, and its codes the other side lacks."""
    return f"""
WITH hr AS (SELECT DISTINCT {UNKNOWN_AS_NULL} AS code FROM employees WHERE {employees}),
fac AS (SELECT DISTINCT dept_code AS code FROM {table} WHERE {space})
SELECT
  (SELECT COUNT(*) FROM hr), (SELECT COUNT(*) FROM hr WHERE code IS NULL),
  (SELECT COUNT(*) FROM hr WHERE code IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM fac WHERE fac.code = hr.code)),
  (SELECT COUNT(*) FROM fac), (SELECT COUNT(*) FROM fac WHERE code IS NULL),
  (SELECT COUNT(*) FROM fac WHERE code IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM hr WHERE hr.code = fac.code))"""


# Salary and trainings by employee name, by hand: each per person (id and name), so the two
# Alexes are two rows; a person with no training reads 0 trainings.
BY_NAME = """
WITH pay AS (SELECT employee_id, employee_name, SUM(salary) AS salary
             FROM employees GROUP BY employee_id, employee_name),
tr AS (SELECT e.employee_id, e.employee_name, COUNT(*) AS trainings
       FROM trainings AS t JOIN employees AS e ON e.employee_id = t.employee_id
       GROUP BY e.employee_id, e.employee_name)
SELECT COALESCE(pay.employee_id, tr.employee_id), COALESCE(pay.employee_name, tr.employee_name),
  pay.salary, CASE WHEN tr.trainings IS NOT NULL THEN tr.trainings ELSE 0 END
FROM pay FULL OUTER JOIN tr
  ON pay.employee_id = tr.employee_id AND pay.employee_name = tr.employee_name"""

# Work orders beside reservations per room and month, January to March, by hand: every room
# either side has in the window, every month; work orders are loaded through February only, so
# their March reads NULL; reservations are loaded into March.
BY_ROOM_AND_MONTH = """
WITH months AS (
  SELECT DATE '2024-01-01' AS period UNION ALL SELECT DATE '2024-02-01'
  UNION ALL SELECT DATE '2024-03-01'),
rooms_seen AS (
  SELECT room_key FROM work_orders
  WHERE opened_at >= TIMESTAMP '2024-01-01 00:00:00' AND opened_at < TIMESTAMP '2024-04-01 00:00:00'
  UNION
  SELECT room_key FROM reservations
  WHERE reserved_at >= TIMESTAMP '2024-01-01 00:00:00'
    AND reserved_at < TIMESTAMP '2024-04-01 00:00:00'),
wo AS (SELECT room_key, CAST(date_trunc('month', opened_at) AS DATE) AS period, COUNT(*) AS n
       FROM work_orders GROUP BY room_key, CAST(date_trunc('month', opened_at) AS DATE)),
rv AS (SELECT room_key, CAST(date_trunc('month', reserved_at) AS DATE) AS period, COUNT(*) AS n
       FROM reservations GROUP BY room_key, CAST(date_trunc('month', reserved_at) AS DATE))
SELECT r.room_key, m.period,
  CASE WHEN m.period < DATE '2024-03-01' THEN COALESCE(wo.n, 0) END, COALESCE(rv.n, 0)
FROM rooms_seen AS r CROSS JOIN months AS m
LEFT JOIN wo ON wo.room_key = r.room_key AND wo.period = m.period
LEFT JOIN rv ON rv.room_key = r.room_key AND rv.period = m.period"""


# Headcount and office area by site, by hand: site is on employees, and a space row counts once,
# whole, in every site its department's people work at (so the area groups overlap); a space
# row whose department has nobody, or that names none, is the NULL site.
BY_SITE = """
WITH hr AS (SELECT site, COUNT(*) AS headcount FROM employees GROUP BY site),
fac AS (
  SELECT site, SUM(area_sqft) AS area
  FROM (SELECT DISTINCT s.space_id, s.area_sqft, e.site
        FROM space AS s LEFT JOIN employees AS e ON e.dept_code = s.dept_code) AS seen
  GROUP BY site)
SELECT COALESCE(hr.site, fac.site), hr.headcount, fac.area
FROM hr FULL OUTER JOIN fac ON hr.site = fac.site"""

# The departments with more than 2 people, each with its people and its area, by hand.
BIG_BY_DEPARTMENT = """
SELECT e.dept_code, COUNT(*),
  (SELECT SUM(area_sqft) FROM space AS s WHERE s.dept_code = e.dept_code)
FROM employees AS e JOIN departments AS d ON d.dept_code = e.dept_code
GROUP BY e.dept_code HAVING COUNT(*) > 2"""


class Depts:
    def __init__(self, backend: str, runtime: Runtime, package: Path, on: dict[str, Any]) -> None:
        self.backend = backend
        self.base = runtime
        self.package = package
        self.on = on  # the package overrides that put a config on this backend

    def sql(self, sql: str) -> list[tuple[Any, ...]]:
        """Hand SQL's rows on this backend's data, sorted, each value plain."""
        return sorted((_plain(row) for row in _rows(self.base, sql)), key=repr)

    def query(self, call: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.base.query(call, now=NOW, **kwargs)

    def answer(self, call: dict[str, Any], **kwargs: Any) -> list[tuple[Any, ...]]:
        return _values(self.query(call, **kwargs))

    def runtime(self, *policies: SemanticPolicyConfig) -> Runtime:
        """A runtime of the package with ``policies`` added, on this backend's data."""
        config: PackageConfig = load_package_config(str(self.package))
        config = replace(config, semantic_policies=[*config.semantic_policies, *policies])
        if self.on:
            config = replace(config, package=replace(config.package, **self.on))
        return Runtime.from_config(config, source_path=str(self.package))


def _plain(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """A row with each number exact and each date as the answer writes it."""
    out = []
    for value in row:
        if isinstance(value, float | Decimal) and not isinstance(value, bool):
            value = Decimal(str(value)).normalize()
        elif hasattr(value, "isoformat") and not isinstance(value, str):
            value = value.isoformat()[:10]
        out.append(value)
    return tuple(out)


AVERAGES = ("average_area", "average_assigned_area")


def _values(out: dict[str, Any]) -> list[tuple[Any, ...]]:
    """The answer's rows, sorted; an average read as an exact number (Postgres answers an
    average's NUMERIC exactly, as a string)."""
    return sorted(
        (
            _plain(
                tuple(
                    Decimal(value) if name in AVERAGES and isinstance(value, str) else value
                    for name, value in row.items()
                )
            )
            for row in out["rows"]
        ),
        key=repr,
    )


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("depts") / "depts"
    shutil.copytree(DEPTS, target)
    return target


@pytest.fixture(scope="module", params=["duckdb", "postgres"])
def depts(request: pytest.FixtureRequest, package: Path) -> Iterator[Depts]:
    if request.param == "duckdb":
        runtime = _runtime(package)
        try:
            _rows(runtime, "SELECT 1")  # the fixture owns the connection
            yield Depts("duckdb", runtime, package, {})
        finally:
            runtime.close()
        return
    required = ("SR_POSTGRES_HOST", "SR_POSTGRES_USER", "SR_POSTGRES_PASSWORD")
    if any(not os.environ.get(name, "").strip() for name in required):
        pytest.skip("postgres: SR_POSTGRES_* unset (run through with_postgres.sh)")
    schema = f"sr_depts_{uuid.uuid4().hex[:12]}"
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

    admin = _runtime(package, **on(options))
    try:
        _rows(admin, f"CREATE SCHEMA {schema}")
    except Exception as exc:
        admin.close()
        if _strict():
            raise
        pytest.skip(f"postgres: unreachable ({type(exc).__name__})")
    overrides = on({**options, "schema": schema})
    runtime = _runtime(package, **overrides)
    try:
        _rows(runtime, SEED)
        yield Depts("postgres", runtime, package, overrides)
    finally:
        runtime.close()
        try:
            _rows(admin, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()


def _note(out: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [row for row in out.get("warnings", []) if row["code"] == code]


def _joined(out: dict[str, Any]) -> dict[str, Any]:
    (note,) = _note(out, "PARTS_JOINED")
    return note


def _counts(note: dict[str, Any]) -> tuple[int, ...]:
    """The joined note's counts per part, in the order the hand counts are written."""
    first, second = note["details"]["parts"]
    return (
        first["rows"],
        first["null_keys"],
        first["unmatched"]["1"],
        second["rows"],
        second["null_keys"],
        second["unmatched"]["0"],
    )


def _refusal(call: Callable[[], Any]) -> tuple[str, str, dict[str, Any]]:
    with pytest.raises(SemanticLayerError) as refused:
        call()
    return refused.value.code, str(refused.value), refused.value.details


def test_both_parts_at_the_key_read_a_full_join_of_their_groups(depts):
    out = depts.query(BY_DEPARTMENT)

    rows = depts.sql(_by_department())
    assert _values(out) == rows
    by_code = {row[0]: row for row in rows if row[0] is not None}
    assert by_code["LAW"][1] == 0  # space and no people: headcount 0
    assert by_code["MKT"][2:] == (0, 0, None)  # people and no space: area 0, average NULL
    # The two NULL-code rows stay two rows, each NULL on the other side.
    assert sorted((row[1:] for row in rows if row[0] is None), key=lambda row: row[0] is None) == [
        (3, None, None, None),
        (None, 900, 1, Decimal("900")),
    ]
    note = _joined(out)
    assert _counts(note) == depts.sql(_department_counts())[0] == (5, 1, 1, 5, 1, 1)
    assert note["message"] == (
        f"The answer joins 2 parts on {DEPT} (parts[0]: {DEPT}, parts[1]: {SPACE_DEPT}), keeping "
        "every key of every part. parts[0] has 5 rows: 1 with no row in parts[1] and 1 with a "
        "NULL key (its own row, NULL in every other part). parts[1] has 5 rows: 1 with no row "
        "in parts[0] and 1 with a NULL key (its own row, NULL in every other part). Summed down "
        "the rows, area adds up. headcount, buildings and average_area are not stated to add "
        "up: a distinct count, an average or a ratio can count a member of two rows twice. "
        f"{CAVEAT}"
    )
    assert note["details"]["keys"] == [
        {"column": DEPT, "parts": [DEPT, SPACE_DEPT], "declared": True}
    ]
    assert note["details"]["keep"] == "all"
    assert note["details"]["kept_rows"] == len(rows)
    # Each part's zero-filled groups, said of its part, with no having (a part has none).
    filled = {row["details"]["part"]: row["message"] for row in _note(out, "ZERO_FILLED_GROUPS")}
    assert filled == {
        0: "parts[0]: 1 of 6 groups has no rows of entity.depts_employee in this call, so its "
        "headcount is 0. A 0 there is data of nothing, not missing data.",
        1: "parts[1]: 1 of 6 groups has no rows of entity.depts_space in this call, so its area "
        "and buildings are 0. A 0 there is data of nothing, not missing data.",
    }
    assert [block["part"] for block in out["parts"]] == [0, 1]
    assert out["query"] == BY_DEPARTMENT


def test_keep_answers_for_the_keys_of_the_filtered_part_only(depts):
    """Headcount of the departments with space in building 32: kept to the facilities part's
    keys, the two departments there and their 3 people; every key of both parts sums every
    person (10), beside area 0 and the NULL-code row's NULL area."""
    kept = depts.query(
        {
            **BY_DEPARTMENT,
            "parts": [HEADCOUNT, IN_BUILDING_32],
            "join": {"on": [[DEPT, SPACE_DEPT]], "keep": [1]},
        }
    )
    every = depts.query(
        {"parts": [HEADCOUNT, IN_BUILDING_32], "join": {"on": [[DEPT, SPACE_DEPT]]}}
    )

    in_32 = "building_key = 32"
    reference = [row[:3] for row in depts.sql(_by_department(space=in_32, keep="fac.present = 1"))]
    assert _values(kept) == reference == [("ENG", 2, 500), ("FIN", 1, 300)]
    assert sum(row[1] for row in reference) == 3
    everything = [row[:3] for row in depts.sql(_by_department(space=in_32))]
    assert _values(every) == everything
    assert sum(row[1] for row in everything) == 10
    assert sorted(row[2] for row in everything if row[2] is not None) == [0, 0, 300, 500]
    assert [row[2] for row in everything if row[0] is None] == [None]
    assert _joined(kept)["details"]["keep"] == [1]
    assert "keeping the keys parts[1] has" in _joined(kept)["message"]


def test_keeping_both_parts_drops_each_null_key_row(depts):
    out = depts.query({**BY_DEPARTMENT, "join": {"on": [[DEPT, SPACE_DEPT]], "keep": [0, 1]}})

    reference = depts.sql(_by_department(keep="hr.present = 1 AND fac.present = 1"))
    assert _values(out) == reference
    assert [row[0] for row in reference] == ["ENG", "FIN", "OPS"]
    # The counts are the whole result's, before keep.
    assert _counts(_joined(out)) == (5, 1, 1, 5, 1, 1)
    assert _joined(out)["details"]["kept_rows"] == 3


def test_two_models_join_on_a_key_column_of_one_name_as_the_callers_choice(depts):
    """Headcount beside facilities' own assignments per department code: no relationship joins
    them, but both models key a dept_code, so the caller may join them. The engine can't check
    that both codes name the same departments, so the answer says the join is the caller's
    and counts each side's codes that found no match: Finance and Marketing have people and no
    assignment (area 0, average NULL), LAB and Legal assignments and no people (headcount 0)."""
    out = depts.query(BY_ASSIGNMENT)

    rows = depts.sql(_by_department(table="assignments"))
    assert _values(out) == rows
    by_code = {row[0]: row for row in rows if row[0] is not None}
    assert by_code["MKT"][2:] == by_code["FIN"][2:] == (0, 0, None)
    assert by_code["LAB"][1] == by_code["LAW"][1] == 0
    assert [row[1:] for row in rows if row[0] is None] == [(3, None, None, None)]
    note = _joined(out)
    counts = depts.sql(_department_counts(table="assignments"))[0]
    assert _counts(note) == counts == (5, 1, 2, 4, 0, 2)
    assert note["details"]["keys"] == [
        {"column": DEPT, "parts": [DEPT, ASSIGNMENT_DEPT], "declared": False}
    ]
    assert note["message"] == (
        f"The answer joins 2 parts on {DEPT} (parts[0]: {DEPT}, parts[1]: {ASSIGNMENT_DEPT}), "
        f"keeping every key of every part. No relationship in the package pairs {DEPT} with "
        f"{ASSIGNMENT_DEPT}: joining on them is the caller's choice. parts[0] has 5 rows: 2 with "
        "no row in parts[1] and 1 with a NULL key (its own row, NULL in every other part). "
        "parts[1] has 4 rows: 2 with no row in parts[0] and 0 with a NULL key (its own row, NULL "
        "in every other part). Summed down the rows, assigned_area adds up. headcount, "
        "assigned_buildings and average_assigned_area are not stated to add up: a distinct "
        f"count, an average or a ratio can count a member of two rows twice. {CAVEAT}"
    )
    # A declared join says nothing of a choice.
    assert "caller's choice" not in _joined(depts.query(BY_DEPARTMENT))["message"]


# Headcount, space and assignments per department code, by hand: every code any side has, each
# side's count or sum 0 where it has no row, and each side's NULL-code row its own row.
THREE_BY_DEPARTMENT = f"""
WITH hr AS (SELECT {UNKNOWN_AS_NULL} AS code, COUNT(*) AS n FROM employees
            GROUP BY {UNKNOWN_AS_NULL}),
sp AS (SELECT dept_code AS code, SUM(area_sqft) AS n FROM space GROUP BY dept_code),
asg AS (SELECT dept_code AS code, SUM(area_sqft) AS n FROM assignments GROUP BY dept_code),
codes AS (SELECT code FROM hr WHERE code IS NOT NULL UNION SELECT code FROM sp
          WHERE code IS NOT NULL UNION SELECT code FROM asg WHERE code IS NOT NULL)
SELECT codes.code, COALESCE(hr.n, 0), COALESCE(sp.n, 0), COALESCE(asg.n, 0)
FROM codes LEFT JOIN hr ON hr.code = codes.code LEFT JOIN sp ON sp.code = codes.code
  LEFT JOIN asg ON asg.code = codes.code
UNION ALL SELECT NULL, n, NULL, NULL FROM hr WHERE code IS NULL
UNION ALL SELECT NULL, NULL, n, NULL FROM sp WHERE code IS NULL
UNION ALL SELECT NULL, NULL, NULL, n FROM asg WHERE code IS NULL"""


def test_three_parts_join_through_a_relationship_and_a_key_column_of_one_name(depts):
    """Headcount, space and assignments per department: space through its declared
    relationship, the assignments by the caller's choice, which the note names alone."""
    out = depts.query(
        {
            "parts": [
                HEADCOUNT,
                {"select": ["measure.depts.area"], "group_by": [SPACE_DEPT]},
                {"select": ["measure.depts.assigned_area"], "group_by": [ASSIGNMENT_DEPT]},
            ],
            "join": {"on": [[DEPT, SPACE_DEPT, ASSIGNMENT_DEPT]]},
        }
    )

    assert _values(out) == depts.sql(THREE_BY_DEPARTMENT)
    assert _joined(out)["details"]["keys"][0]["declared"] is False
    assert (
        f"No relationship in the package pairs {DEPT} with {ASSIGNMENT_DEPT}: joining on them is "
        "the caller's choice."
    ) in _joined(out)["message"]


def test_a_foreign_key_joins_a_key_column_of_one_name(depts):
    """The employees' own dept_code (a foreign key, UNASSIGNED read as written) beside the
    assignments' key column: one of the two models keys it, so the caller may join them."""
    employees = {"select": ["measure.depts.headcount"], "group_by": [EMPLOYEE_DEPT]}
    out = depts.query(
        {"parts": [employees, ASSIGNED], "join": {"on": [[EMPLOYEE_DEPT, ASSIGNMENT_DEPT]]}}
    )

    rows = depts.sql(_by_department(table="assignments", code="dept_code"))
    assert _values(out) == rows
    assert {row[0]: row[1:3] for row in rows if row[0] is not None}["UNASSIGNED"] == (1, 0)
    assert _joined(out)["details"]["keys"][0]["declared"] is False


def test_keep_answers_for_the_assigned_departments_only(depts):
    """Headcount of the departments with an assignment in building 32: Engineering's 2."""
    in_32 = {
        **ASSIGNED,
        "where": [{"field": "dimension.depts_assignment_building_key", "op": "is", "value": 32}],
    }
    out = depts.query(
        {"parts": [HEADCOUNT, in_32], "join": {"on": [[DEPT, ASSIGNMENT_DEPT]], "keep": [1]}}
    )

    reference = depts.sql(
        _by_department(space="building_key = 32", keep="fac.present = 1", table="assignments")
    )
    assert _values(out) == reference
    assert [row[:3] for row in reference] == [("ENG", 2, 500)]


def test_a_row_filter_reads_the_visible_rows_of_a_join_by_the_callers_choice(depts):
    """A south site lead, the assignments listed as shared: rows and counts are the hand join
    of the visible rows only. Unshared, the assignments part is refused as it is alone."""
    shared = depts.runtime(_site_filter(shared=["entity.depts_assignment"]))
    held = depts.runtime(_site_filter())
    try:
        out = shared.query(BY_ASSIGNMENT, now=NOW, context=SITE_LEAD)
        south = "site = 'south'"
        assert _values(out) == depts.sql(_by_department(employees=south, table="assignments"))
        assert (
            _counts(_joined(out))
            == depts.sql(_department_counts(employees=south, table="assignments"))[0]
            == (3, 1, 0, 4, 0, 2)
        )
        refused = _refusal(lambda: held.query(BY_ASSIGNMENT, now=NOW, context=SITE_LEAD))
        code, message, details = _refusal(lambda: held.query(ASSIGNED, now=NOW, context=SITE_LEAD))
        assert refused == (code, f"parts[1]: {message}", {**details, "part": 1})
        assert code == "POLICY_DENIED" and details["reason"] == "row_filter_uncovered_root"
    finally:
        shared.close()
        held.close()


def test_a_hidden_model_is_refused_as_a_model_that_isnt_there(package, tmp_path):
    """A caller who can't see the assignments is refused the join exactly as a package with no
    assignments refuses it: the gate never says what a hidden model holds or keys."""
    without = tmp_path / "depts"
    shutil.copytree(package, without)
    (without / "models" / "assignments.yml").unlink()
    graph = without / "graph.yml"
    graph.write_text(
        "".join(
            line
            for line in graph.read_text(encoding="utf-8").splitlines(keepends=True)
            if "assignment:" not in line
        ),
        encoding="utf-8",
    )
    config = load_package_config(str(package))
    policy = visibility_policy("hidden", "entity.depts_assignment")
    hidden = Runtime.from_config(
        replace(config, semantic_policies=[*config.semantic_policies, policy]),
        source_path=str(package),
    )
    absent = _runtime(without)
    support = RequestContext(roles=("support",))
    try:
        refused = _refusal(lambda: hidden.query(BY_ASSIGNMENT, now=NOW, context=support))
        assert refused == _refusal(lambda: absent.query(BY_ASSIGNMENT, now=NOW))
        assert refused[0] == "OBJECT_NOT_FOUND" and refused[2]["part"] == 1
        # Beside a part the caller sees, the join they can make answers as it does without it.
        alexes = {
            "parts": [
                {"select": ["measure.depts.salary"], "group_by": [NAME]},
                {"select": ["measure.depts.trainings"], "group_by": [NAME]},
            ],
            "join": {"on": [[NAME, NAME]]},
        }
        seen = hidden.query(alexes, now=NOW, context=support)
        plain = absent.query(alexes, now=NOW)
        for key in ("rows", "warnings", "assumptions", "parts"):
            assert seen[key] == plain[key]
    finally:
        hidden.close()
        absent.close()


def test_two_people_named_alex_stay_two_rows(depts):
    """Both parts grouped by a display name group by the employee key beside it, and the key
    joins with the name: two Alexes are two rows, never one or four."""
    out = depts.query(
        {
            "parts": [
                {"select": ["measure.depts.salary"], "group_by": [NAME]},
                {"select": ["measure.depts.trainings"], "group_by": [NAME]},
            ],
            "join": {"on": [[NAME, NAME]]},
        }
    )

    assert _values(out) == depts.sql(BY_NAME)
    assert [row[1:] for row in _values(out) if row[1] == "Alex"] == [
        ("Alex", 100, 2),
        ("Alex", 120, 0),
    ]
    assert _joined(out)["details"]["keys"] == [
        {"column": EMPLOYEE, "parts": [EMPLOYEE, EMPLOYEE], "declared": True},
        {"column": NAME, "parts": [NAME, NAME], "declared": True},
    ]
    assert [row["code"] for row in out["assumptions"]].count("GROUPED_BY_KEY") == 2


def test_a_timed_call_joins_each_period_and_settles_each_part_by_its_own_load(depts):
    out = depts.query(
        {"parts": [WORK_ORDERS, RESERVATIONS], "join": {"on": [[ROOM, ROOM]]}, "time": MONTHS}
    )

    assert [
        (row[ROOM], row["period"], row["work_orders"], row["reservations"]) for row in out["rows"]
    ] == sorted(depts.sql(BY_ROOM_AND_MONTH), key=lambda row: (row[1], row[0]))
    march = [row for row in out["rows"] if row["period"] == "2024-03-01"]
    assert {row["work_orders"] for row in march} == {None}  # loaded through February only
    assert {row[ROOM]: row["work_orders"] for row in out["rows"] if row["period"] == "2024-02-01"}[
        "R3"
    ] == 0
    note = _joined(out)
    assert [key["column"] for key in note["details"]["keys"]] == [ROOM, "period"]
    windows = [part["window"] for part in note["details"]["parts"]]
    assert all(window["start"] == "2024-01-01" for window in windows)
    assert {row["details"]["part"] for row in _note(out, "NO_DATA_YET")} == {0}
    # A measure named inside an expression reads its clock as the bare id does.
    opened = {"select": [{"expression": "measure.depts.work_orders", "as": "opened"}]}
    renamed = depts.query(
        {
            "parts": [{**WORK_ORDERS, **opened}, RESERVATIONS],
            "join": {"on": [[ROOM, ROOM]]},
            "time": MONTHS,
        }
    )
    assert [row["opened"] for row in renamed["rows"]] == [row["work_orders"] for row in out["rows"]]


def test_a_part_whose_groups_overlap_is_not_stated_to_add_up(depts):
    """Headcount and office area by site: a department's space counts in every site its people
    work at, so the area rows sum to more than the space there is, and the note says area
    doesn't add up, as the part's own overlap notice does."""
    out = depts.query(
        {
            "parts": [
                {**HEADCOUNT, "group_by": [SITE]},
                {**SPACE, "select": ["measure.depts.area"], "group_by": [SITE]},
            ],
            "join": {"on": [[SITE, SITE]]},
        }
    )

    assert _values(out) == depts.sql(BY_SITE)
    assert sum(row["area"] for row in out["rows"]) == 3500
    assert depts.sql("SELECT SUM(area_sqft) FROM space") == [(2400,)]
    note = _joined(out)
    assert {row["column"]: row["adds_up"] for row in note["details"]["columns"]} == {
        "headcount": False,
        "area": False,
    }
    assert "Summed down the rows" not in note["message"]
    assert note["message"].endswith(
        "headcount and area are not stated to add up: a distinct count, an average, a ratio or "
        "a part's overlapping groups can count a member of two rows twice. Each part was "
        "aggregated to its own groups before the join, so no row counts twice within a group of "
        "a part; a row of parts[1] counts in every group it has a row in, as its REWRITE_APPLIED "
        "note says, so those groups overlap. It does not check that the joined columns mean the "
        "same thing in both models; the join is the caller's."
    )
    (overlap,) = [
        row for row in _note(out, "REWRITE_APPLIED") if row["details"].get("overlapping_groups")
    ]
    assert overlap["details"]["part"] == 1
    assert overlap["message"].startswith("parts[1]: ")
    assert "so the groups overlap and don't add up to the total" in overlap["message"]


# Rooms per department in the shop package, by hand: a room's department is on its rows (a
# link), so a room with rows in two departments is in both.
ROOMS_BY_DEPARTMENT = """
SELECT department, COUNT(DISTINCT room_id) FROM room_rows WHERE room_id IS NOT NULL
GROUP BY department"""


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_a_part_grouped_across_a_link_is_not_stated_to_add_up(request, backend):
    """Rooms per department, where a room can belong to two: the rows sum to 6 of 5 rooms, so
    the count is not stated to add up, alone or beside the rooms' area."""
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    department = "dimension.shop_room_rows_department"
    rooms = {"select": ["measure.shop.room_count"], "group_by": [department]}
    area = {"select": ["measure.shop.room_area"], "group_by": [department]}
    one = runtime.query({"parts": [rooms], "join": {"on": [[department]]}}, now=NOW)
    two = runtime.query(
        {"parts": [rooms, area], "join": {"on": [[department, department]]}}, now=NOW
    )

    assert _values(one) == sorted(source.reference(ROOMS_BY_DEPARTMENT), key=repr)
    assert sum(row["room_count"] for row in one["rows"]) == 6
    assert source.reference("SELECT COUNT(DISTINCT room_id) FROM room_rows") == [(5,)]
    assert _joined(one)["details"]["columns"] == [
        {"column": "room_count", "part": 0, "adds_up": False}
    ]
    assert "Summed down the rows" not in _joined(one)["message"]
    assert [row["adds_up"] for row in _joined(two)["details"]["columns"]] == [False, False]
    assert "a row of parts[0] and parts[1] counts in every group" in _joined(two)["message"]
    (link,) = [row for row in _note(one, "REWRITE_APPLIED") if row["details"]["part"] == 0]
    assert link["details"]["overlapping_groups"] == {"link": True}
    assert "Across a link it counts in every group it has a row in" in link["message"]


def test_an_entity_filter_in_a_kept_part_answers_its_keys_only(depts):
    """Departments with more than 2 people, beside their area: with the filtered part named in
    keep, the answer is those departments, each with its real headcount and area."""
    out = depts.query(
        {
            **BY_DEPARTMENT,
            "parts": [
                {**HEADCOUNT, "where": [BIG_DEPARTMENTS]},
                {**SPACE, "select": ["measure.depts.area"]},
            ],
            "join": {"on": [[DEPT, SPACE_DEPT]], "keep": [0]},
        }
    )

    assert _values(out) == depts.sql(BIG_BY_DEPARTMENT) == [("OPS", 3, 400)]


def test_a_row_filter_reads_each_parts_visible_rows_and_counts_no_hidden_row(depts):
    """A south site lead sees the south employees only; facilities space is listed as shared.
    Marketing's one person works north: hidden, it is neither a row nor an unmatched key."""
    shared = depts.runtime(_site_filter(shared=["entity.depts_space"]))
    held = depts.runtime(_site_filter())
    try:
        out = shared.query(BY_DEPARTMENT, now=NOW, context=SITE_LEAD)
        south = "site = 'south'"
        assert _values(out) == depts.sql(_by_department(employees=south))
        assert _counts(_joined(out)) == depts.sql(_department_counts(employees=south))[0]
        assert _counts(_joined(out)) == (3, 1, 0, 5, 1, 2)
        # Each part's rows are what it answers alone for the same caller.
        alone = sorted(
            (
                _plain(tuple(row.values()))
                for row in shared.query(HEADCOUNT, now=NOW, context=SITE_LEAD)["rows"]
            ),
            key=repr,
        )
        joined = {row[0]: row[1] for row in _values(out)}
        assert all(joined[code] == headcount for code, headcount in alone if code is not None)

        refused = _refusal(lambda: held.query(BY_DEPARTMENT, now=NOW, context=SITE_LEAD))
        code, message, details = _refusal(lambda: held.query(SPACE, now=NOW, context=SITE_LEAD))
        assert refused == (
            code,
            f"parts[1]: {message}",
            {**details, "part": 1},
        )
        assert code == "POLICY_DENIED" and details["reason"] == "row_filter_uncovered_root"
    finally:
        shared.close()
        held.close()


def test_a_part_reads_the_callers_view(depts):
    """A caller who can't see the facilities model is refused the joined call exactly as the
    facilities part alone, said of that part."""
    hidden = depts.runtime(visibility_policy("hidden", "entity.depts_space"))
    support = RequestContext(roles=("support",))
    try:
        refused = _refusal(lambda: hidden.query(BY_DEPARTMENT, now=NOW, context=support))
        code, message, details = _refusal(lambda: hidden.query(SPACE, now=NOW, context=support))
        assert refused == (code, f"parts[1]: {message}", {**details, "part": 1})
        assert code == "OBJECT_NOT_FOUND"
        assert hidden.query(HEADCOUNT, now=NOW, context=support)["rows"]
    finally:
        hidden.close()


def test_one_part_answers_as_the_part_alone(depts):
    """Rows and every disclosure of the part alone, each said of parts[0]."""
    alone = depts.query({**WORK_ORDERS, "time": MONTHS})
    out = depts.query({"parts": [WORK_ORDERS], "join": {"on": [[ROOM]]}, "time": MONTHS})

    assert out["rows"] == alone["rows"]
    stated = {(row["code"], row["message"]) for row in out["warnings"]}
    assert {(row["code"], f"parts[0]: {row['message']}") for row in alone["warnings"]} <= stated
    assert [(row["code"], row["message"]) for row in out["assumptions"]] == [
        (row["code"], f"parts[0]: {row['message']}") for row in alone["assumptions"]
    ]
    assert out["parts"][0]["window"] == alone["window"]


def test_a_limit_states_the_whole_results_counts_and_its_ties(depts):
    out = depts.query(
        {**BY_DEPARTMENT, "order_by": [{"field": "headcount", "direction": "desc"}], "limit": 1}
    )

    assert out["row_count"] == 1
    assert out["rows"][0]["headcount"] == 3  # Operations and the NULL-code row both have 3
    assert [row["code"] for row in out["warnings"]].count("TIES_AT_LIMIT") == 1
    assert _counts(_joined(out)) == (5, 1, 1, 5, 1, 1)
    assert _joined(out)["details"]["kept_rows"] == 7


def test_validate_and_sql_state_the_join_without_running_it(depts):
    checked = depts.base.validate(BY_DEPARTMENT, now=NOW, verbosity="minimal")
    compiled = depts.base.compile(BY_DEPARTMENT, now=NOW, verbosity="minimal")

    assert checked["ok"] and compiled["ok"]
    for out in (checked, compiled):
        note = _joined(out)
        assert "kept_rows" not in note["details"]
        assert note["details"]["keys"] == [
            {"column": DEPT, "parts": [DEPT, SPACE_DEPT], "declared": True}
        ]
        assert [block["part"] for block in out["parts"]] == [0, 1]
    assert compiled["rendered_sql"].count("FULL OUTER JOIN joined_part_1 AS part_1") == 1


def test_the_execute_tool_runs_a_joined_call(depts):
    out = SemanticLayerMCPAdapter(depts.base).call_tool(
        "execute", {"query": BY_DEPARTMENT, "now": NOW}
    )

    assert _values(out) == depts.sql(_by_department())


def test_a_package_snapshot_test_reads_a_joined_call(depts):
    """A package's own snapshot test of a joined call compares its rows (a timed one by its
    periods) as a call's."""
    timed = {"parts": [WORK_ORDERS, RESERVATIONS], "join": {"on": [[ROOM, ROOM]]}, "time": MONTHS}
    expected = [dict(row) for row in depts.query(timed)["rows"]]
    spec = {"kind": "query_matches_snapshot", "query": timed, "expected_rows": expected}
    assert _run_test(depts.base, "joined", spec, NOW) == {"id": "joined", "ok": True}
    wrong = [{**expected[0], "reservations": 99}, *expected[1:]]
    failed = _run_test(depts.base, "joined", {**spec, "expected_rows": wrong}, NOW)
    assert failed["error"]["code"] == "SNAPSHOT_MISMATCH"


def test_parts_on_clocks_in_two_zones_are_refused(tmp_path):
    """Reservations dated in New York beside work orders dated in UTC: one statement buckets
    every part's months in one zone, so the call is refused, not answered in either."""
    package = tmp_path / "depts"
    shutil.copytree(DEPTS, package)
    reservations = package / "models" / "reservations.yml"
    model = yaml.safe_load(reservations.read_text(encoding="utf-8"))
    model["model"]["times"]["reserved_at"].update(
        {"timezone": "America/New_York", "column_timezone": "UTC"}
    )
    reservations.write_text(yaml.safe_dump(model, sort_keys=False), encoding="utf-8")
    runtime = _runtime(package)
    try:
        call = {
            "parts": [WORK_ORDERS, RESERVATIONS],
            "join": {"on": [[ROOM, ROOM]]},
            "time": MONTHS,
        }
        code, message, details = _refusal(lambda: runtime.query(call, now=NOW))
    finally:
        runtime.close()
    assert (code, message) == (
        "INVALID_QUERY",
        "parts: the parts read clocks in different time zones (America/New_York, UTC): one "
        "statement buckets every part in one zone",
    )
    assert details["reason"] == "parts_time_zones_differ"


def test_a_metric_grant_is_refused_a_joined_call():
    store = "dimension.jaffle_store_name"
    metrics = ("metric.sales.repeat_customer_orders", "metric.sales.high_value_customer_orders")
    call = {
        "parts": [{"select": [metric], "group_by": [store]} for metric in metrics],
        "join": {"on": [[store, store]]},
    }
    runtime = Runtime("jaffle_shop")
    try:
        grant = RequestContext(metric_allowlist=metrics, dimension_allowlist=(store,))
        refused = _refusal(lambda: runtime.query(call, now="2017-09-04T12:00:00Z", context=grant))
    finally:
        runtime.close()
    assert refused[:2] == ("RESOURCE_ACCESS_DENIED", "Resource access is not permitted.")


REFUSALS: list[tuple[str, dict[str, Any], str, str, dict[str, Any]]] = [
    (
        "having in a part",
        {
            **BY_DEPARTMENT,
            "parts": [
                {**HEADCOUNT, "having": [{"field": "headcount", "op": "gt", "value": 1}]},
                SPACE,
            ],
        },
        "INVALID_QUERY",
        "parts[0].having: a part has no having: a group it removed would read as 0 beside the "
        "other parts",
        {"path": "parts[0].having"},
    ),
    (
        "time in a part",
        {**BY_DEPARTMENT, "parts": [HEADCOUNT, {**SPACE, "time": {"grain": "month"}}]},
        "INVALID_QUERY",
        "parts[1].time: a joined call has one time block, on the call, read by every part whose "
        "measures have a time role",
        {"path": "parts[1].time"},
    ),
    (
        "limit in a part",
        {**BY_DEPARTMENT, "parts": [{**HEADCOUNT, "limit": 3}, SPACE]},
        "INVALID_QUERY",
        "parts[0].limit: a part has no order_by, limit or totals; they belong to the joined call",
        {"path": "parts[0].limit"},
    ),
    (
        "totals in a part",
        {**BY_DEPARTMENT, "parts": [{**HEADCOUNT, "totals": []}, SPACE]},
        "INVALID_QUERY",
        "parts[0].totals: a part has no order_by, limit or totals; they belong to the joined call",
        {"path": "parts[0].totals"},
    ),
    (
        "a part's key beside parts",
        {**BY_DEPARTMENT, "where": []},
        "INVALID_QUERY",
        "where: a joined call has ['parts', 'join', 'time', 'order_by', 'limit']; where belongs "
        "to a part",
        {"path": "where"},
    ),
    (
        "a part that is a joined call",
        {**BY_DEPARTMENT, "parts": [BY_DEPARTMENT, SPACE]},
        "INVALID_QUERY",
        "parts[0]: a part is a call of its own, not a joined call",
        {"path": "parts[0]"},
    ),
    (
        "no parts",
        {"parts": [], "join": {"on": [[DEPT]]}},
        "INVALID_QUERY",
        "parts: is a list of one or more calls",
        {"path": "parts"},
    ),
    (
        "a part that isn't a call",
        {"parts": ["measure.depts.headcount"], "join": {"on": [[DEPT]]}},
        "INVALID_QUERY",
        "parts[0]: is a call: an object",
        {"path": "parts[0]"},
    ),
    (
        "an unknown key in a part",
        {**BY_DEPARTMENT, "parts": [{**HEADCOUNT, "filters": []}, SPACE]},
        "INVALID_QUERY",
        "parts[0].filters: unknown key; a part has ['select', 'group_by', 'where', 'routes', "
        "'observation_scope']",
        {"path": "parts[0].filters"},
    ),
    (
        "an unknown key beside parts",
        {**BY_DEPARTMENT, "sort": []},
        "INVALID_QUERY",
        "sort: unknown key; a joined call has ['parts', 'join', 'time', 'order_by', 'limit']",
        {"path": "sort"},
    ),
    (
        "a retired key beside parts",
        {**BY_DEPARTMENT, "metric_filters": []},
        "INVALID_QUERY",
        "metric_filters: is not part of a query call",
        {"path": "metric_filters"},
    ),
    (
        "an unknown key in join",
        {**BY_DEPARTMENT, "join": {"on": [[DEPT, SPACE_DEPT]], "how": "left"}},
        "INVALID_QUERY",
        "join.how: unknown key; the keys are ['on', 'keep']",
        {"path": "join.how"},
    ),
    (
        "no tuple",
        {**BY_DEPARTMENT, "join": {"on": []}},
        "INVALID_QUERY",
        "join.on: join.on names at least one tuple, one dimension of every part",
        {"path": "join.on"},
    ),
    (
        "a column joined twice",
        {**BY_DEPARTMENT, "join": {"on": [[DEPT, SPACE_DEPT], [DEPT, SPACE_DEPT]]}},
        "INVALID_QUERY",
        f"join.on[1]: names {DEPT} of parts[0] again (join.on[0])",
        {"path": "join.on[1]", "reason": "join_key_repeated", "part": 0},
    ),
    (
        "an entity filter in a part keep doesn't name",
        {**BY_DEPARTMENT, "parts": [{**HEADCOUNT, "where": [BIG_DEPARTMENTS]}, SPACE]},
        "INVALID_QUERY",
        "parts[0].where[0]: an entity filter in a part that join.keep doesn't name: a key it "
        "removes would read as 0 beside the other parts; add 0 to join.keep",
        {"path": "parts[0].where[0]", "reason": "part_filter_not_kept", "part": 0},
    ),
    (
        "an entity filter in the part keep leaves out",
        {
            "parts": [
                HEADCOUNT,
                {
                    **SPACE,
                    "where": [
                        {
                            **BIG_DEPARTMENTS,
                            "having": [{"field": "measure.depts.area", "op": "gt", "value": 450}],
                        }
                    ],
                },
            ],
            "join": {"on": [[DEPT, SPACE_DEPT]], "keep": [0]},
        },
        "INVALID_QUERY",
        "parts[1].where[0]: an entity filter in a part that join.keep doesn't name: a key it "
        "removes would read as 0 beside the other parts; add 1 to join.keep",
        {"path": "parts[1].where[0]", "reason": "part_filter_not_kept", "part": 1},
    ),
    (
        "no join",
        {"parts": [HEADCOUNT, SPACE]},
        "INVALID_QUERY",
        "join: a joined call names join.on: one dimension of every part per key column",
        {"path": "join"},
    ),
    (
        "a tuple short of a part",
        {**BY_DEPARTMENT, "join": {"on": [[DEPT]]}},
        "INVALID_QUERY",
        "join.on[0]: join.on names at least one tuple, one dimension of every part: 2 dimension "
        "ids, in the order of parts",
        {"path": "join.on[0]"},
    ),
    (
        "keep naming no part",
        {**BY_DEPARTMENT, "join": {"on": [[DEPT, SPACE_DEPT]], "keep": [2]}},
        "INVALID_QUERY",
        "join.keep: join.keep names part indexes 0..1, or 'all'",
        {"path": "join.keep"},
    ),
    (
        "a join column the part doesn't group by",
        {**BY_DEPARTMENT, "join": {"on": [[DEPT, BUILDING]]}},
        "INVALID_QUERY",
        f"join.on[0]: names {BUILDING}, which parts[1] doesn't group by; a part joins on its own "
        "group_by columns",
        {"path": "join.on[0]", "reason": "join_key_not_grouped", "part": 1},
    ),
    (
        "two foreign keys no relationship pairs",
        {
            "parts": [{"select": ["measure.depts.headcount"], "group_by": [EMPLOYEE_DEPT]}, SPACE],
            "join": {"on": [[EMPLOYEE_DEPT, SPACE_DEPT]]},
        },
        "INVALID_QUERY",
        f"join.on[0]: pairs {EMPLOYEE_DEPT} with {SPACE_DEPT}: no declared relationship pairs "
        "'dept_code' of 'entity.depts_employee' with 'dept_code' of 'entity.depts_space', and "
        "neither model declares 'dept_code' in its key. Parts join on the same dimension in "
        "every part, on the two columns a declared relationship joins, or on identifier columns "
        "of one name and type that one of the models declares in its key",
        {"path": "join.on[0]", "reason": "join_key_not_shared"},
    ),
    (
        "a name column",
        {
            "parts": [
                {"select": ["measure.depts.headcount"], "group_by": [DEPT_NAME]},
                {"select": ["measure.depts.area"], "group_by": [BUILDING]},
            ],
            "join": {"on": [[DEPT_NAME, BUILDING]]},
        },
        "INVALID_QUERY",
        f"join.on[0]: pairs {DEPT_NAME} with {BUILDING}: 'dept_name' doesn't say it identifies "
        "anything: a join column names an identifier (a code, key or id) and what it identifies. "
        "Parts join on the same dimension in every part, on the two columns a declared "
        "relationship joins, or on identifier columns of one name and type that one of the "
        "models declares in its key",
        {"path": "join.on[0]", "reason": "join_key_not_shared"},
    ),
    (
        "a finer part",
        {
            "parts": [
                {**IN_BUILDING_32, "where": [], "group_by": [SPACE_DEPT, BUILDING]},
                HEADCOUNT,
            ],
            "join": {"on": [[SPACE_DEPT, DEPT]]},
        },
        "INVALID_QUERY",
        f"parts[0]: groups by {BUILDING} beyond the join key ({SPACE_DEPT}): joined, each of its "
        "rows would repeat beside the other parts' rows. Every part is grouped by the join key "
        "only",
        {"path": "parts[0]", "reason": "parts_grain_mismatch", "part": 0},
    ),
    (
        "an untimed part under a timed call",
        {"parts": [WORK_ORDERS, HEADCOUNT], "join": {"on": [[ROOM, DEPT]]}, "time": MONTHS},
        "INVALID_QUERY",
        "parts[1]: selects nothing with a clock, so the joined call's time can't apply to it: a "
        "part with no time role beside a timed joined call isn't answered yet",
        {"path": "parts[1]", "reason": "part_untimed"},
    ),
    (
        "two parts selecting one name",
        {
            **BY_DEPARTMENT,
            "parts": [
                HEADCOUNT,
                {
                    "select": [{"expression": "measure.depts.area", "as": "headcount"}],
                    "group_by": [SPACE_DEPT],
                },
            ],
        },
        "DUPLICATE_OUTPUT_ALIAS",
        "parts[1].select[0]: parts[1] selects 'headcount' and so does parts[0]",
        {"path": "parts[1].select[0]", "part": 1},
    ),
    (
        "an order by no column",
        {**BY_DEPARTMENT, "order_by": [{"field": "nope"}]},
        "INVALID_ORDER_BY",
        "order_by[0].field: names 'nope', which is no column of the joined answer",
        {"path": "order_by[0].field"},
    ),
    (
        "a part's own refusal",
        {**BY_DEPARTMENT, "parts": [HEADCOUNT, {**SPACE, "select": ["measure.depts.nope"]}]},
        "OBJECT_NOT_FOUND",
        "parts[1]: Unknown measure 'measure.depts.nope'",
        {"part": 1},
    ),
]


@pytest.mark.parametrize(
    ("call", "code", "message", "details"),
    [case[1:] for case in REFUSALS],
    ids=[case[0] for case in REFUSALS],
)
def test_a_joined_call_the_engine_cant_answer_is_refused_at_its_path(
    depts, call, code, message, details
):
    refused = _refusal(lambda: depts.query(call))

    assert refused[:2] == (code, message)
    assert {key: refused[2].get(key) for key in details} == details
