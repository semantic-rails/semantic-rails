"""Planner names kept while planner modules still import them.

The planner reads the caller's visible view (``visible_view``), which holds nothing hidden from
them, so each of these is the identity.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from .. import visible_view

_F = TypeVar("_F", bound=Callable[..., Any])


def with_dimension_visibility(operation: _F) -> _F:
    return operation


def caller_hidden_ids(config: Any) -> frozenset[str]:
    return frozenset()


def visible_object_ids(config: Any, object_ids: Iterable[str], **_: Any) -> list[str]:
    return list(object_ids)


def visible_dimensions(config: Any) -> list[Any]:
    return list(config.dimensions)


def visible_value_domains(config: Any) -> list[Any]:
    return list(config.value_domains)


def require_visible_objects(config: Any, query: dict[str, Any], resolved: Any) -> None:
    return None


def discovery_query(query: dict[str, Any]) -> dict[str, Any]:
    """Keep caller authority on internal discovery, out of portable Query IR."""
    pinned = visible_view.pinned()
    return {**query, "policy_context": pinned.policy_context} if pinned is not None else query
