"""One caller clock for intent parsing, all candidates, and readiness checks."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from typing import Any, ParamSpec, TypeVar

_context: ContextVar[dict[str, Any] | None] = ContextVar("planner_time_reference", default=None)
_runtime: ContextVar[Any] = ContextVar("planner_time_runtime", default=None)
_default_zone: ContextVar[str] = ContextVar("planner_default_time_zone", default="UTC")
_P = ParamSpec("_P")
_R = TypeVar("_R")


def time_policy_context(context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """An explicit query context takes precedence over the current plan's clock."""

    return context if context is not None else _context.get()


def time_timezone(role: str = "", *, runtime: Any = None) -> str:
    """The selected role's zone, or the package default before a role is chosen."""

    runtime = runtime if runtime is not None else _runtime.get()
    config = getattr(runtime, "_config", None)
    if role:
        selected = next(
            (row for row in getattr(config, "temporal_roles", []) if row.id == role), None
        )
        return str(getattr(selected, "timezone", "") or "UTC")
    if runtime is _runtime.get():
        return _default_zone.get()
    snapshot = getattr(runtime, "_snapshot", None)
    defaults = getattr(snapshot, "normalized", {}).get("defaults", {}) or {}
    return str((defaults.get("time") or {}).get("timezone") or "UTC")


def with_time_reference(operation: Callable[_P, _R]) -> Callable[_P, _R]:
    """Carry the clock through helpers without mutating the shared runtime."""

    @wraps(operation)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        runtime = args[0] if args else kwargs.get("runtime")
        zone = time_timezone(runtime=runtime)
        partial = kwargs.get("partial_query")
        context = (
            partial.get("policy_context")
            if isinstance(partial, dict)
            else kwargs.get("policy_context")
        )
        context = context if isinstance(context, dict) else None
        token = _context.set(time_policy_context(context))
        runtime_token = _runtime.set(runtime)
        zone_token = _default_zone.set(zone)
        try:
            return operation(*args, **kwargs)
        finally:
            _default_zone.reset(zone_token)
            _runtime.reset(runtime_token)
            _context.reset(token)

    return wrapped
