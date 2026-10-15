"""Nested compiles: lowering a query sometimes needs another query's SQL.

An entity filter's per-entity source, a distribution's per-entity values, a branch of a
combined answer and a parent lookup's settled values are each compiled as a query of their
own. The lowering asks for them here instead of importing the compiler, which imports the
lowering. ``semantic_rails.compiler`` registers itself on import.

A nested compile runs inside the outer compile's recording contexts, so the objects, routes,
temporal roles, rollup scans and stock key gaps it reads reach the outer binding, which policy
and disclosures read.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..schema import PackageConfig
from ..sql_ast import SqlSelect


class NestedCompiler(Protocol):
    def query_sql(
        self,
        config: PackageConfig,
        payload: dict[str, Any],
        *,
        project_cut: bool = False,
        guard_empty: bool = True,
    ) -> SqlSelect:
        """A nested query's SQL; ``guard_empty=False`` for a distribution's per-entity values."""
        ...

    def predicate_source_sql(self, config: PackageConfig, payload: dict[str, Any]) -> SqlSelect:
        """The per-entity values a metric predicate reads, settled like any query's."""
        ...


_compiler: NestedCompiler | None = None


def register(compiler: NestedCompiler) -> None:
    global _compiler
    _compiler = compiler


def _registered() -> NestedCompiler:
    if _compiler is None:
        raise RuntimeError("Import semantic_rails.compiler: it registers the nested compiles.")
    return _compiler


def query_sql(
    config: PackageConfig,
    payload: dict[str, Any],
    *,
    project_cut: bool = False,
    guard_empty: bool = True,
) -> SqlSelect:
    return _registered().query_sql(
        config, payload, project_cut=project_cut, guard_empty=guard_empty
    )


def predicate_source_sql(config: PackageConfig, payload: dict[str, Any]) -> SqlSelect:
    return _registered().predicate_source_sql(config, payload)
