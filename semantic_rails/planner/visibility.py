"""Caller-scoped dimension visibility shared by every planner candidate path."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
from types import EllipsisType
from typing import Any, ParamSpec, TypeVar

from .. import policies
from ..compiler import bind_query
from ..errors import SemanticLayerError
from ..expressions import collect_object_references
from ..policy_rules import visible_object_ids as _visible_object_ids


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
        runtime: Any = args[0] if args else kwargs["runtime"]
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


def caller_hidden_ids(config: Any) -> frozenset[str] | None:
    """The pinned caller's hidden object ids; ``None`` means visibility is uncertain."""

    current = _visibility.get()
    if current is not None and current.config is config:
        return current.hidden_ids
    return policies.diagnostic_hidden_object_ids(config, {})


def visible_object_ids(
    config: Any,
    object_ids: Iterable[str],
    *,
    hidden_ids: frozenset[str] | None | EllipsisType = ...,
) -> list[str]:
    """Filter candidates before ranking or text; explicit uncertainty withholds all."""

    if isinstance(hidden_ids, EllipsisType):
        hidden_ids = caller_hidden_ids(config)
    return _visible_object_ids(config, object_ids, hidden_ids=hidden_ids)


def visible_dimensions(config: Any) -> list[Any]:
    """Filter before any score, selection, candidate count or diagnostic text."""

    visible_ids = set(visible_object_ids(config, (row.id for row in config.dimensions)))
    return [row for row in getattr(config, "dimensions", []) if row.id in visible_ids]


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


def require_visible_objects(
    config: Any, query: dict[str, Any], resolved: list[dict[str, Any]]
) -> None:
    """Refuse a bypassing draft before its IR or diagnostics can be returned."""

    ids = [
        row.id
        for rows in (
            config.entities,
            config.dimensions,
            config.temporal_roles,
            config.relationships,
            config.measures,
            config.metric_recipes,
        )
        for row in rows
    ]
    # Caller references take their ordinary unknown-id path, including nested validation.
    forbidden = (
        set(ids) - set(visible_object_ids(config, ids)) - policies.caller_named_object_ids(config)
    )
    if not forbidden:
        return

    try:
        references = set(bind_query(config, None, query).object_ids) if query else set()
    except SemanticLayerError:
        # Invalid drafts still go through normal validation. Direct authored
        # references must not become a diagnostic bypass when binding fails.
        references = set(collect_object_references(query, config))
    references.update(str(row["id"]) for row in resolved if row.get("id"))
    if forbidden & references:
        raise SemanticLayerError("OBJECT_NOT_FOUND", "The requested object was not found.")
