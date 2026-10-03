"""Caller-scoped dimension visibility shared by every planner candidate path."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from .. import policies
from ..compiler import bind_query
from ..errors import SemanticLayerError
from ..expressions import collect_object_references


@dataclass(frozen=True)
class _Visibility:
    config: Any
    hidden_ids: frozenset[str] | None
    policy_context: Mapping[str, Any] | None


_visibility: ContextVar[_Visibility | None] = ContextVar(
    "planner_dimension_visibility", default=None
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


def with_dimension_visibility(operation: Callable[_P, _R]) -> Callable[_P, _R]:
    """Pin visibility for one plan, including nested fallback discovery calls."""

    @wraps(operation)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        runtime: Any = args[0]
        partial = kwargs.get("partial_query")
        partial = partial if isinstance(partial, Mapping) else {}
        current = _visibility.get()
        if (
            current is not None
            and current.config is runtime._config
            and "policy_context" not in partial
        ):
            return operation(*args, **kwargs)
        context = partial.get("policy_context", {})
        token = _visibility.set(
            _Visibility(
                runtime._config,
                policies.diagnostic_hidden_object_ids(runtime._config, context),
                context,
            )
        )
        try:
            return operation(*args, **kwargs)
        finally:
            _visibility.reset(token)

    return wrapped


def visible_dimensions(config: Any) -> list[Any]:
    """Filter before any score, selection, candidate count or diagnostic text."""

    current = _visibility.get()
    hidden = (
        current.hidden_ids
        if current is not None and current.config is config
        else policies.diagnostic_hidden_object_ids(config, {})
    )
    return [
        row
        for row in getattr(config, "dimensions", [])
        if hidden is not None and row.id not in hidden
    ]


def discovery_query(query: dict[str, Any]) -> dict[str, Any]:
    """Keep caller authority on internal discovery, out of portable Query IR."""

    current = _visibility.get()
    return {**query, "policy_context": current.policy_context} if current is not None else query


def visible_value_domains(config: Any) -> list[Any]:
    """A named value cannot reintroduce a hidden dimension via its domain."""

    visible_ids = {row.id for row in visible_dimensions(config)}
    return [
        replace(domain, dimensions=dimensions)
        for domain in getattr(config, "value_domains", [])
        if (dimensions := [dim for dim in domain.dimensions if dim in visible_ids])
    ]


def require_visible_dimensions(
    config: Any, query: dict[str, Any], resolved: list[dict[str, Any]]
) -> None:
    """Refuse a bypassing draft before its IR or diagnostics can be returned."""

    visible_ids = {row.id for row in visible_dimensions(config)}
    forbidden = {row.id for row in config.dimensions} - visible_ids
    if not forbidden:
        return

    try:
        references = set(bind_query(config, None, query).object_ids) if query else set()
    except SemanticLayerError:
        # Invalid drafts still go through normal validation. Direct authored
        # references must not become a diagnostic bypass when binding fails.
        references = set(collect_object_references(query, config))
    references.update(row.get("id") for row in resolved if row.get("id"))
    if forbidden & references:
        raise SemanticLayerError("OBJECT_NOT_FOUND", "The requested dimension was not found.")
