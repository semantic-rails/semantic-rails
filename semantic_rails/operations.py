"""The operations the transports serve: one row each, naming the service that answers it and
the arguments it reads beside the runtime.

The HTTP routes (``/api/v1/<name>``) and the MCP tools dispatch through :func:`run`; each
transport still reads its own wire format into these arguments. Who is asking reaches an
operation only as ``context`` (a call's) or ``policy_context`` (a metadata seed's, already in
the engine's form), and the request's time only as ``now``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .catalog_service import resolve_catalog
from .metadata import build_options_payload, discover_payload, inspect_payload, valid_values_payload
from .planner import plan_payload
from .runtime import Runtime

_CALL = ("query", "now", "context", "verbosity", "sql_profile")


@dataclass(frozen=True)
class Operation:
    name: str
    # Called with the runtime first, then ``arguments`` by keyword.
    service: Callable[..., dict[str, Any]]
    arguments: tuple[str, ...]


def _method(name: str) -> Callable[..., dict[str, Any]]:
    """A runtime method, looked up on the runtime each call; a call is its first argument."""

    def service(runtime: Runtime, **arguments: Any) -> dict[str, Any]:
        method = getattr(runtime, name)
        if "query" in arguments:
            return method(arguments.pop("query"), **arguments)
        return method(**arguments)

    service.__name__ = f"Runtime.{name}"
    return service


OPERATIONS: dict[str, Operation] = {
    row.name: row
    for row in (
        Operation("validate", _method("validate"), _CALL),
        Operation("compile", _method("compile"), _CALL),
        Operation("query", _method("query"), (*_CALL, "limits")),
        Operation(
            "catalog",
            resolve_catalog,
            ("view", "verbosity", "kind", "search", "entity", "policy_context"),
        ),
        Operation(
            "discover",
            discover_payload,
            ("terms", "kinds", "partial_query", "stage", "verbosity", "limit", "enforce_scope"),
        ),
        Operation("inspect", inspect_payload, ("object_id", "partial_query", "verbosity")),
        Operation(
            "build-options",
            build_options_payload,
            (
                "partial_query",
                "focus_terms",
                "focus_object_id",
                "step",
                "stage",
                "verbosity",
                "include_blocked",
                "limit",
            ),
        ),
        Operation(
            "valid-values",
            valid_values_payload,
            (
                "dimension_id",
                "query",
                "search",
                "limit",
                "offset",
                "include_counts",
                "allow_live_query",
            ),
        ),
        Operation("plan", plan_payload, ("intent", "partial_query", "now", "detail", "limit")),
        Operation(
            "segment-validate", _method("segment_validate"), ("segment_id", "policy_context")
        ),
        Operation("segment-explain", _method("segment_explain"), ("segment_id", "policy_context")),
        Operation(
            "segment-preview", _method("segment_preview"), ("segment_id", "limit", "policy_context")
        ),
    )
}


def run(name: str, runtime: Runtime, **arguments: Any) -> dict[str, Any]:
    """Answer the operation ``name`` with ``arguments``, which must be among its own."""
    operation = OPERATIONS[name]
    unknown = sorted(set(arguments) - set(operation.arguments))
    if unknown:
        raise TypeError(f"Operation {name!r} takes no {unknown}; it takes {operation.arguments}")
    return operation.service(runtime, **arguments)
