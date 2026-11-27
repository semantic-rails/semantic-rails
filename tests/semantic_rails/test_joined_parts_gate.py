"""``join_allowed``: which columns a joined call may join its parts on.

Two parts join on the same dimension, on the two columns a declared single-column relationship
joins, or on columns of one name that one of their models declares in its key (the caller's
choice: ``undeclared`` names them), each an identifier (a code, key or id named for what it
identifies) of one type. A column of one name that neither model keys is not enough, nor one
that only dates the rows.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from semantic_rails.compiler_parts.joined_parts import join_allowed, undeclared


def _dimension(id_: str, entity: str, column: str, data_type: str = "string") -> Any:
    return SimpleNamespace(id=id_, entity=entity, column=column, data_type=data_type)


def _relationship(source: str, source_column: str, target: str, target_column: str) -> Any:
    return SimpleNamespace(
        link=None,
        source_entity=source,
        source_column=source_column,
        source_columns=[],
        target_entity=target,
        target_column=target_column,
        target_columns=[],
    )


def _entity(id_: str, table: str, *key: str) -> Any:
    return SimpleNamespace(id=id_, table=table, key_columns=list(key))


CONFIG: Any = SimpleNamespace(
    entities=[
        _entity("entity.department", "departments", "dept_code"),
        _entity("entity.employee", "employees", "employee_id"),
        _entity("entity.space", "space", "space_id"),
        _entity("entity.badge", "badges", "id"),
        # Facilities' own rows, keyed by building and department code, related to nothing.
        _entity("entity.assignment", "assignments", "building_key", "DEPT_CODE"),
        _entity("entity.budget", "budgets", "budget_key"),
        _entity("entity.snapshot", "snapshots", "dept_code", "load_date_key"),
        _entity("entity.calendar", "calendar", "date_key"),
        _entity("entity.sale", "sales", "sale_id"),
    ],
    dimensions=[
        _dimension("dimension.department_code", "entity.department", "dept_code"),
        _dimension("dimension.department_name", "entity.department", "dept_name"),
        _dimension("dimension.employee_dept", "entity.employee", "dept_code"),
        _dimension("dimension.space_dept", "entity.space", "dept_code"),
        _dimension("dimension.space_dept_id", "entity.space", "dept_code", "id"),
        _dimension("dimension.space_floor", "entity.space", "floor_code", "integer"),
        _dimension("dimension.employee_id", "entity.employee", "id", "id"),
        _dimension("dimension.badge_id", "entity.badge", "id", "id"),
        _dimension("dimension.assignment_dept", "entity.assignment", "dept_code"),
        _dimension("dimension.budget_dept", "entity.budget", "dept_code"),
        _dimension("dimension.budget_dept_number", "entity.budget", "dept_code", "integer"),
        _dimension("dimension.budget_department", "entity.budget", "department_code"),
        _dimension("dimension.snapshot_load", "entity.snapshot", "load_date_key", "date"),
        _dimension("dimension.space_load", "entity.space", "load_date_key", "date"),
        _dimension("dimension.calendar_date", "entity.calendar", "date_key", "date"),
        _dimension("dimension.sale_date", "entity.sale", "date_key", "date"),
    ],
    relationships=[
        _relationship("entity.employee", "dept_code", "entity.department", "dept_code"),
        _relationship("entity.space", "dept_code", "entity.department", "dept_code"),
    ],
)


@pytest.mark.parametrize(
    ("dimensions", "reason"),
    [
        (["dimension.department_name", "dimension.department_name"], ""),
        (["dimension.department_code", "dimension.employee_dept"], ""),
        (["dimension.space_dept", "dimension.department_code"], ""),
        # An id-typed column holds its key's values, whatever type they are.
        (["dimension.space_dept_id", "dimension.department_code"], ""),
        # A column of one name that one of the two models keys: the caller's choice.
        (["dimension.department_code", "dimension.assignment_dept"], ""),
        (["dimension.employee_dept", "dimension.assignment_dept"], ""),
        (["dimension.budget_dept", "dimension.assignment_dept"], ""),
        (
            ["dimension.department_code", "dimension.employee_dept", "dimension.assignment_dept"],
            "",
        ),
        # A calendar keyed by its date is what a date column identifies.
        (["dimension.sale_date", "dimension.calendar_date"], ""),
        (
            ["dimension.employee_dept", "dimension.space_dept"],
            "no declared relationship pairs 'dept_code' of 'entity.employee' with 'dept_code' of "
            "'entity.space', and neither model declares 'dept_code' in its key",
        ),
        (
            ["dimension.employee_dept", "dimension.space_dept", "dimension.budget_dept"],
            "no declared relationship pairs 'dept_code' of 'entity.employee' with 'dept_code' of "
            "'entity.space', and no model of the tuple declares 'dept_code' in its key",
        ),
        (
            ["dimension.assignment_dept", "dimension.budget_department"],
            "no declared relationship pairs 'dept_code' of 'entity.assignment' with "
            "'department_code' of 'entity.budget', and their names differ",
        ),
        (
            ["dimension.snapshot_load", "dimension.space_load"],
            "no declared relationship pairs 'load_date_key' of 'entity.snapshot' with "
            "'load_date_key' of 'entity.space', and 'load_date_key' only dates the rows",
        ),
        (
            ["dimension.budget_dept_number", "dimension.assignment_dept"],
            "the columns' types differ (integer, string)",
        ),
        (
            ["dimension.department_name", "dimension.space_dept"],
            "'dept_name' doesn't say it identifies anything: a join column names an identifier "
            "(a code, key or id) and what it identifies",
        ),
        (
            ["dimension.employee_id", "dimension.badge_id"],
            "'id' alone doesn't say what it identifies",
        ),
        (
            ["dimension.department_code", "dimension.space_floor"],
            "the columns' types differ (string, integer)",
        ),
        (
            ["dimension.department_code", "dimension.gone"],
            "a dimension it names is not one the parts can read",
        ),
    ],
    ids=[
        "the same dimension",
        "a declared relationship",
        "a declared relationship read the other way",
        "an id-typed column",
        "a key column of one name",
        "a foreign key beside a key column of one name",
        "a column of one name keyed by the second model",
        "three parts, a relationship and a key column of one name",
        "a calendar's date",
        "two foreign keys of one code",
        "three columns of one name none keys",
        "two names",
        "a date that only dates the rows",
        "one name, two types",
        "a name column",
        "a bare id",
        "two types",
        "an unknown dimension",
    ],
)
def test_the_gate_joins_only_what_the_package_says_names_the_same_thing(dimensions, reason):
    assert join_allowed(CONFIG, dimensions) == reason


@pytest.mark.parametrize(
    ("dimensions", "chosen"),
    [
        (["dimension.department_name", "dimension.department_name"], []),
        (["dimension.department_code", "dimension.employee_dept"], []),
        (["dimension.space_dept_id", "dimension.department_code"], []),
        (["dimension.department_code", "dimension.assignment_dept"], ["dimension.assignment_dept"]),
        (
            ["dimension.department_code", "dimension.employee_dept", "dimension.assignment_dept"],
            ["dimension.assignment_dept"],
        ),
        (
            ["dimension.assignment_dept", "dimension.department_code", "dimension.employee_dept"],
            ["dimension.department_code", "dimension.employee_dept"],
        ),
    ],
    ids=[
        "the same dimension",
        "a declared relationship",
        "an id-typed column",
        "a key column of one name",
        "a relationship and a key column of one name",
        "the chosen column first",
    ],
)
def test_a_join_on_key_columns_of_one_name_is_the_callers_choice(dimensions, chosen):
    assert join_allowed(CONFIG, dimensions) == ""
    assert undeclared(CONFIG, dimensions) == chosen
