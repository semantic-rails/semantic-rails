"""Object identities resolved while constructing a bound query's SQL AST.

This records compiler resolutions, never request strings or literal contents.
Candidate planning runs outside the scope so rejected paths do not become reads.
ContextVar keeps concurrent and recursively lowered queries isolated.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

from ..schema import (
    DimensionConfig,
    MeasureConfig,
    PackageConfig,
    RelationshipConfig,
    TemporalRoleConfig,
)


@dataclass
class BindingDependencies:
    object_ids: set[str] = field(default_factory=set)
    indexes: dict[tuple[int, int], Any] = field(default_factory=dict)


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
    ids = dependencies.object_ids
    ids.add(row.id)
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
