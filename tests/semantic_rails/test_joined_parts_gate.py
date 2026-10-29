"""``join_allowed``: which columns a joined call may join its parts on.

Two parts join on the same dimension, or on the two columns a declared single-column
relationship joins, each an identifier (a code, key or id named for what it identifies) of one
type. A column of the same name in two models is not enough.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from semantic_rails.compiler_parts.joined_parts import join_allowed


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


CONFIG: Any = SimpleNamespace(
    entities=[
        SimpleNamespace(id="entity.department", table="departments"),
        SimpleNamespace(id="entity.employee", table="employees"),
        SimpleNamespace(id="entity.space", table="space"),
        SimpleNamespace(id="entity.badge", table="badges"),
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
        (
            ["dimension.employee_dept", "dimension.space_dept"],
            "no declared relationship pairs 'dept_code' of 'entity.employee' with 'dept_code' of "
            "'entity.space'",
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
        "two foreign keys of one code",
        "a name column",
        "a bare id",
        "two types",
        "an unknown dimension",
    ],
)
def test_the_gate_joins_only_what_the_package_says_names_the_same_thing(dimensions, reason):
    assert join_allowed(CONFIG, dimensions) == reason
