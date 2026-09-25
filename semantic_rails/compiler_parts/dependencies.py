"""Object identities resolved while constructing a bound query's SQL AST.

This records compiler resolutions, never request strings or literal contents.
Candidate planning runs outside the scope so rejected paths do not become reads.
ContextVar keeps concurrent and recursively lowered queries isolated.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

from ..ir import LogicalPlan
from ..schema import (
    DimensionConfig,
    MeasureConfig,
    PackageConfig,
    RelationshipConfig,
    TemporalRoleConfig,
)


@dataclass
class Cut:
    objects: set[str] = field(default_factory=set)
    # Root leaves being lowered when the cut was recorded; None is the whole query.
    owners: frozenset[str] | None = None
    # Root leaves read inside the cut. A whole-query cut makes their cuts whole-query.
    leaves: set[str] = field(default_factory=set)


@dataclass
class BindingDependencies:
    object_ids: set[str] = field(default_factory=set)
    indexes: dict[tuple[int, int], Any] = field(default_factory=dict)
    cuts: list[Cut] = field(default_factory=list)
    temporal_roles: dict[str, set[str]] = field(default_factory=dict)
    unresolved: set[str] = field(default_factory=set)


@dataclass
class PlanBindings:
    leaves: dict[str, set[str]] = field(default_factory=dict)
    temporal_roles: dict[str, set[str]] = field(default_factory=dict)
    measures: dict[str, list[set[str]]] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    recipe_owners: dict[str, set[str]] = field(default_factory=dict)
    project_cut: bool = False
    root: bool = False


_plan: ContextVar[PlanBindings | None] = ContextVar("plan_bindings", default=None)
_targets: ContextVar[tuple[set[str], ...]] = ContextVar("binding_targets", default=())
_role_targets: ContextVar[tuple[set[str], ...]] = ContextVar("role_targets", default=())
_recipes: ContextVar[tuple[str, ...]] = ContextVar("binding_recipes", default=())
_cut_owners: ContextVar[frozenset[str] | None] = ContextVar("cut_owners", default=None)
_open_cuts: ContextVar[tuple[Cut, ...]] = ContextVar("open_cuts", default=())


def record_ids(ids: set[str]) -> None:
    dependencies = _active.get()
    if dependencies is not None:
        dependencies.object_ids.update(ids)
        for target in _targets.get():
            target.update(ids)


@contextmanager
def capture_objects(target: set[str]) -> Iterator[None]:
    token = _targets.set((*_targets.get(), target))
    try:
        yield
    finally:
        _targets.reset(token)


@contextmanager
def binding_cut() -> Iterator[None]:
    dependencies = _active.get()
    cut = Cut(owners=_cut_owners.get())
    if dependencies is not None:
        dependencies.cuts.append(cut)
    token = _open_cuts.set((*_open_cuts.get(), cut))
    try:
        with capture_objects(cut.objects):
            yield
    finally:
        _open_cuts.reset(token)


@contextmanager
def cut_owners(*aliases: str) -> Iterator[None]:
    """Attribute cuts to the root leaves being lowered; no aliases means the whole query.

    Nested compilations keep their parent's attribution, so an unscoped cut
    always counts for the whole query.
    """
    plan = _plan.get()
    root = plan is not None and plan.root
    token = _cut_owners.set((frozenset(aliases) or None) if root else _cut_owners.get())
    try:
        yield
    finally:
        _cut_owners.reset(token)


def measure_cut_owners(measure_id: str) -> AbstractContextManager[None]:
    """Narrow a cut inside one measure's expression to that measure's root leaves."""
    plan = _plan.get()
    return cut_owners(*(plan.aliases.get(measure_id, ()) if plan is not None else ()))


@contextmanager
def plan_bindings(plan: LogicalPlan, *, project_cut: bool = False) -> Iterator[PlanBindings]:
    bindings = PlanBindings(project_cut=project_cut, root=_plan.get() is None)
    for bound in plan.bound_measures:
        target = bindings.leaves.setdefault(bound.alias, set())
        bindings.measures.setdefault(bound.measure_id, []).append(target)
        bindings.aliases.setdefault(bound.measure_id, []).append(bound.alias)
    token = _plan.set(bindings)
    try:
        yield bindings
    finally:
        _plan.reset(token)


def project_is_cut() -> bool:
    plan = _plan.get()
    return plan is not None and plan.project_cut


@contextmanager
def measure_objects(measure_id: str) -> Iterator[None]:
    plan = _plan.get()
    targets = plan.measures.get(measure_id, []) if plan is not None else []
    token = _targets.set((*_targets.get(), *targets))
    try:
        yield
    finally:
        _targets.reset(token)


def record_leaf_reference(alias: str) -> None:
    plan = _plan.get()
    if plan is not None:
        if alias in plan.leaves:
            plan.recipe_owners.setdefault(alias, set()).update(_recipes.get())
            record_ids(plan.leaves[alias])
            if plan.root:
                for cut in _open_cuts.get():
                    cut.leaves.add(alias)
            for recipe_id in _recipes.get():
                for role_id in plan.temporal_roles.get(alias, ()):
                    record_temporal_role(recipe_id, role_id)
        else:
            dependencies = _active.get()
            if dependencies is not None:
                dependencies.unresolved.add(alias)


@contextmanager
def leaf_objects(alias: str) -> Iterator[None]:
    plan = _plan.get()
    target = plan.leaves.setdefault(alias, set()) if plan is not None else set()
    with capture_objects(target):
        yield


def record_temporal_role(object_id: str, role_id: str, *, leaf_alias: str = "") -> None:
    dependencies = _active.get()
    if dependencies is not None and role_id:
        dependencies.temporal_roles.setdefault(object_id, set()).add(role_id)
        plan = _plan.get()
        if plan is not None and leaf_alias:
            plan.temporal_roles.setdefault(leaf_alias, set()).add(role_id)
        for target in _role_targets.get():
            target.add(role_id)


@contextmanager
def leaf_predicate_roles(*aliases: str) -> Iterator[None]:
    """Retain effective roles from actual compilation of leaf-owned predicates."""
    plan = _plan.get()
    roles: set[str] = set()
    token = _role_targets.set((*_role_targets.get(), roles))
    try:
        yield
    finally:
        _role_targets.reset(token)
    if plan is not None:
        for alias in aliases:
            plan.temporal_roles.setdefault(alias, set()).update(roles)
            # Optimized operands can resolve their recipe before its predicates
            # compile; normal projections resolve it afterwards from the leaf.
            for recipe_id in plan.recipe_owners.get(alias, ()):
                for role_id in roles:
                    record_temporal_role(recipe_id, role_id)


@contextmanager
def recipe_objects(recipe_id: str) -> Iterator[None]:
    roles: set[str] = set()
    token = _recipes.set((*_recipes.get(), recipe_id))
    role_token = _role_targets.set((*_role_targets.get(), roles))
    try:
        yield
    finally:
        _role_targets.reset(role_token)
        _recipes.reset(token)
    for role_id in roles:
        record_temporal_role(recipe_id, role_id)


_active: ContextVar[BindingDependencies | None] = ContextVar("binding_dependencies", default=None)


@contextmanager
def binding_dependencies() -> Iterator[BindingDependencies]:
    dependencies = BindingDependencies()
    token = _active.set(dependencies)
    try:
        yield dependencies
    finally:
        _active.reset(token)


@contextmanager
def candidate_planning() -> Iterator[None]:
    """Do not authorize alternative paths merely examined by the planner."""
    token = _active.set(None)
    try:
        yield
    finally:
        _active.reset(token)


def record_bound_object(row: Any, config: PackageConfig) -> None:
    dependencies = _active.get()
    if dependencies is None or row is None:
        return
    ids = {row.id}
    if isinstance(row, MeasureConfig):
        ids.update(
            value for value in (row.entity, row.subject_entity, row.aggregation_entity) if value
        )
    elif isinstance(row, DimensionConfig):
        ids.add(row.entity)
    elif isinstance(row, TemporalRoleConfig):
        ids.add(row.dimension)
        dimension = next((item for item in config.dimensions if item.id == row.dimension), None)
        if dimension is not None:
            ids.add(dimension.entity)
    elif isinstance(row, RelationshipConfig):
        ids.update((row.source_entity, row.target_entity))

    record_ids(ids)


T = TypeVar("T")


class _BindingIndex(dict[str, T]):
    def __init__(self, source: dict[str, T], config: PackageConfig):
        super().__init__(source)
        self.config = config

    def __getitem__(self, key: str) -> T:
        row = super().__getitem__(key)
        record_bound_object(row, self.config)
        return row

    def get(self, key: str, default: Any = None) -> Any:
        row = super().get(key, default)
        if key in self:
            record_bound_object(row, self.config)
        return row


def binding_index(source: dict[str, T], config: PackageConfig) -> dict[str, T]:
    dependencies = _active.get()
    if dependencies is None:
        return source
    key = (id(source), id(config))
    if key not in dependencies.indexes:
        dependencies.indexes[key] = _BindingIndex(source, config)
    return dependencies.indexes[key]
