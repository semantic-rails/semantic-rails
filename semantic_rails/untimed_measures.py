"""Measures with no time role, and the package line that gives one its clock.

A measure is timed by its own ``times:`` or its model's ``default: true`` time. One with neither
answers without ``time``, but every call that puts it on a grain or a window is refused with
``INCOMPATIBLE_TEMPORAL_ROLE``. When its rows reach a clock by to-one links (a fact's day key into
a calendar), one line fixes that: ``times: [<that clock>]`` on the measure. This module finds that
line for the refusal, and names the measures that lack one for validation and the promotion
check (``MEASURE_WITHOUT_TIME_ROLE``), from the configuration alone.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from .schema import PackageConfig

MEASURE_WITHOUT_TIME_ROLE = "MEASURE_WITHOUT_TIME_ROLE"


def _role_entities(config: PackageConfig) -> dict[str, str]:
    """Each temporal role's entity: the entity of the dimension backing it."""
    dimensions = {row.id: row.entity for row in config.dimensions}
    return {
        role.id: dimensions[role.dimension]
        for role in config.temporal_roles
        if role.dimension in dimensions
    }


def _to_one_reach(config: PackageConfig, start: str) -> set[str]:
    """The entities ``start``'s rows each reach one row of, by to-one hops (``N:1`` forward,
    ``1:1`` either way), ``start`` included."""
    steps: dict[str, set[str]] = {}
    for rel in config.relationships:
        if rel.cardinality in {"N:1", "1:1"}:
            steps.setdefault(rel.source_entity, set()).add(rel.target_entity)
        if rel.cardinality == "1:1":
            steps.setdefault(rel.target_entity, set()).add(rel.source_entity)
    reached, pending = {start}, deque([start])
    while pending:
        for entity in steps.get(pending.popleft(), set()) - reached:
            reached.add(entity)
            pending.append(entity)
    return reached


def _where(config: PackageConfig, measure: Any) -> tuple[str, str]:
    """The measure's key and its model's id, as the package files spell them."""
    model = next((row.model_id for row in config.entities if row.id == measure.entity), "")
    return str(measure.id).rsplit(".", 1)[-1], str(model or "")


def reachable_clocks(config: PackageConfig, measure: Any) -> list[str]:
    """The temporal roles whose entity the measure's rows reach one row of: its own entity's
    first, then the package's default clocks, each in package order."""
    reach = _to_one_reach(config, measure.entity)
    entities = _role_entities(config)
    own = [role for role, entity in entities.items() if entity == measure.entity]
    defaults = {role.id for role in config.temporal_roles if role.default_query_time_axis}
    others = [
        role
        for role, entity in entities.items()
        if entity != measure.entity and entity in reach and role in defaults
    ]
    return own + others


def untimed_fix(config: PackageConfig, measure: Any, role: str) -> tuple[str, dict[str, Any]]:
    """What gives an untimed measure the clock ``role``: one sentence, and the line it names
    (``{model, measure, times}``; empty when no to-one route reaches the role)."""
    key, model = _where(config, measure)
    entity = _role_entities(config).get(role, "")
    declare = {"model": model, "measure": key, "times": [role]}
    where = f"measure {key} in model {model}" if model else f"measure {key}"
    if entity and entity == measure.entity:
        return (
            f"Its model declares that time: mark it `default: true` there, or add `times: "
            f"[{role}]` to {where}",
            declare,
        )
    if entity and entity in _to_one_reach(config, measure.entity):
        return (
            f"Its rows reach that clock by to-one links: if it dates them, add `times: "
            f"[{role}]` to {where} (Architect suggest_model with project_path drafts it)",
            declare,
        )
    label = next((row.label or row.id for row in config.entities if row.id == entity), entity)
    return (
        f"Its rows reach no one {label} by to-one links, so first link its model to {label} "
        f"(Architect suggest_links proposes links from the data), then add `times: [{role}]` "
        f"to {where}",
        {},
    )


def untimed_warnings(config: PackageConfig, source_path: Path | str) -> list[dict[str, Any]]:
    """One ``MEASURE_WITHOUT_TIME_ROLE`` warning per measure that has no time role although its
    rows reach a clock: it can't be asked by month, and the warning gives the line that fixes
    it. A measure that reaches no clock is left alone: the package says nothing of its time."""
    warnings = []
    for measure in config.measures:
        if measure.compatible_temporal_roles or str(measure.id).startswith("measure.__"):
            continue
        clocks = reachable_clocks(config, measure)
        if not clocks:
            continue
        key, model = _where(config, measure)
        where = f"measure {key} in model {model}" if model else f"measure {key}"
        listed = ", ".join(clocks)
        warnings.append(
            {
                "code": MEASURE_WITHOUT_TIME_ROLE,
                "message": (
                    f"{source_path}: {measure.id} has no time role, so it cannot be asked by "
                    f"month or in a window, although its rows reach {listed}. If "
                    + ("it dates" if len(clocks) == 1 else "one of them dates")
                    + f" its rows, add `times: [{clocks[0] if len(clocks) == 1 else '<it>'}]` "
                    f"to {where}."
                ),
                "details": {
                    "measure": measure.id,
                    "model": model,
                    "reachable_temporal_roles": clocks,
                },
            }
        )
    return warnings
