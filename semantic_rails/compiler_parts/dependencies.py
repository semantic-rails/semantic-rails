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
    retained_filtered_series: set[str] = field(default_factory=set)
    project_cut: bool = False
    root: bool = False
    # Settles its empty groups as a call of its own (the request's, or a total's coarser call):
    # loaded coverage, observation and time scopes, whatever it answers for.
    settles: bool = False
    # A distribution's per-entity values: its entities are those with rows of the input's
    # measures, so no filtered leaf keeps the source groups its own filter removed.
    entity_values: bool = False


_plan: ContextVar[PlanBindings | None] = ContextVar("plan_bindings", default=None)
_targets: ContextVar[tuple[set[str], ...]] = ContextVar("binding_targets", default=())
_role_targets: ContextVar[tuple[set[str], ...]] = ContextVar("role_targets", default=())
_recipes: ContextVar[tuple[str, ...]] = ContextVar("binding_recipes", default=())
_cut_owners: ContextVar[frozenset[str] | None] = ContextVar("cut_owners", default=None)
_open_cuts: ContextVar[tuple[Cut, ...]] = ContextVar("open_cuts", default=())
# Whom a read lowered now answers for, for its disclosures: root leaf or output aliases; an
# empty set is the whole answer (a query-level filter); None, no owner was declared.
_read_owners: ContextVar[frozenset[str] | None] = ContextVar("read_owners", default=None)


def record_ids(ids: set[str]) -> None:
    dependencies = _active.get()
    if dependencies is not None:
        dependencies.object_ids.update(ids)
        for target in _targets.get():
            target.update(ids)


@contextmanager
def holding(var: ContextVar[T], value: T) -> Iterator[None]:
    """Hold ``var`` at ``value`` for the block, then restore it."""
    token = var.set(value)
    try:
        yield
    finally:
        var.reset(token)


@contextmanager
def capture_objects(target: set[str]) -> Iterator[None]:
    with holding(_targets, (*_targets.get(), target)):
        yield


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
    with (
        holding(_cut_owners, (frozenset(aliases) or None) if root else _cut_owners.get()),
        owning(*aliases),
    ):
        yield


@contextmanager
def owning(*aliases: str) -> Iterator[None]:
    """Attribute the reads lowered in this block to these root leaves or outputs (none: the
    whole answer). A nested compile keeps its parent's owners."""
    plan = _plan.get()
    root = plan is not None and plan.root
    with holding(_read_owners, frozenset(aliases) if root else _read_owners.get()):
        yield


@contextmanager
def reading_for(*outputs: str) -> Iterator[None]:
    """Attribute the reads of a nested compile lowered in this block to these outputs of the
    request (a total's coarser call answers for the total)."""
    with holding(_read_owners, frozenset(outputs)):
        yield


def current_read_owners() -> frozenset[str] | None:
    """Whom a read lowered now answers for (see ``owning``); None when nobody declared it."""
    return _read_owners.get()


def measure_cut_owners(measure_id: str) -> AbstractContextManager[None]:
    """Narrow a cut inside one measure's expression to that measure's root leaves."""
    plan = _plan.get()
    return cut_owners(*(plan.aliases.get(measure_id, ()) if plan is not None else ()))


@contextmanager
def plan_bindings(
    plan: LogicalPlan,
    *,
    project_cut: bool = False,
    standalone: bool = False,
    entity_values: bool = False,
) -> Iterator[PlanBindings]:
    """The bindings of a plan being lowered: the request's own (``root``), or nested in it. A
    ``standalone`` nested plan, or a distribution's ``entity_values``, settles its empty groups
    as a call of its own would, while its reads still answer for the request's outputs."""
    root = _plan.get() is None
    bindings = PlanBindings(
        project_cut=project_cut,
        root=root,
        settles=root or standalone or entity_values,
        entity_values=entity_values,
    )
    for bound in plan.bound_measures:
        target = bindings.leaves.setdefault(bound.alias, set())
        bindings.measures.setdefault(bound.measure_id, []).append(target)
        bindings.aliases.setdefault(bound.measure_id, []).append(bound.alias)
    with holding(_plan, bindings):
        yield bindings


def plan_is_root() -> bool:
    """Whether the query being lowered is the request's own, not one nested inside it."""
    plan = _plan.get()
    return plan is not None and plan.root


def settles_as_a_call() -> bool:
    """Whether the query being lowered settles its empty groups as a call of its own (the
    request's, or a standalone nested call: ``plan_bindings``)."""
    plan = _plan.get()
    return plan is not None and plan.settles


def lowers_entity_values() -> bool:
    """Whether the plan being lowered is a distribution's per-entity values (``plan_bindings``)."""
    plan = _plan.get()
    return plan is not None and plan.entity_values


def retains_filtered_groups() -> bool:
    """Whether a filtered leaf may keep its source groups (``_preserves_filtered_series``): in a
    call that settles its empty groups, but not in a distribution's per-entity values."""
    return settles_as_a_call() and not lowers_entity_values()


def record_retained_filtered_series(alias: str) -> None:
    plan = _plan.get()
    if plan is not None and plan.root:
        plan.retained_filtered_series.add(alias)


def project_is_cut() -> bool:
    plan = _plan.get()
    return plan is not None and plan.project_cut


@contextmanager
def measure_objects(measure_id: str) -> Iterator[None]:
    plan = _plan.get()
    targets = plan.measures.get(measure_id, []) if plan is not None else []
    with holding(_targets, (*_targets.get(), *targets)):
        yield


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
    with holding(_role_targets, (*_role_targets.get(), roles)):
        yield
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
    with holding(_active, dependencies):
        yield dependencies


@contextmanager
def candidate_planning() -> Iterator[None]:
    """Do not authorize alternative paths merely examined by the planner."""
    with holding(_active, None):
        yield


def record_bound_object(row: Any, config: PackageConfig) -> None:
    dependencies = _active.get()
    if dependencies is None or row is None:
        return
    ids = {row.id}
    if isinstance(row, (MeasureConfig, DimensionConfig)):
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
