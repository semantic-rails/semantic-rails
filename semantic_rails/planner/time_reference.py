"""One caller clock for intent parsing, all candidates, and readiness checks."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from typing import Any, ParamSpec, TypeVar

_context: ContextVar[dict[str, Any] | None] = ContextVar("planner_time_reference", default=None)
_P = ParamSpec("_P")
_R = TypeVar("_R")


def time_policy_context(context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """An explicit query context takes precedence over the current plan's clock."""

    return context if context is not None else _context.get()


def with_time_reference(operation: Callable[_P, _R]) -> Callable[_P, _R]:
    """Carry the clock through helpers without mutating the shared runtime."""

    @wraps(operation)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        partial = kwargs.get("partial_query")
        context = (
            partial.get("policy_context")
            if isinstance(partial, dict)
            else kwargs.get("policy_context")
        )
        context = context if isinstance(context, dict) else None
        token = _context.set(time_policy_context(context))
        try:
            return operation(*args, **kwargs)
        finally:
            _context.reset(token)

    return wrapped
