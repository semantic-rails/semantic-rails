"""Shared, policy-safe catalog resolution for HTTP and MCP transports."""

from __future__ import annotations

from typing import Any

from . import visible_view
from .metadata import catalog_payload
from .request_context import context_from_policy_context
from .runtime import Runtime, runtime_request_scope


@runtime_request_scope
def resolve_catalog(
    runtime: Runtime,
    *,
    view: str = "summary",
    verbosity: str = "compact",
    kind: str = "",
    search: str = "",
    entity: str = "",
    policy_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use a manifest only for an unfiltered, context-free catalog.

    Actor, tenant, project, roles, environment, and audience may all become
    policy inputs. Any populated context therefore forces live evaluation;
    a precomputed anonymous catalog must never cross that trust boundary.
    The manifest is built on the view of a caller with no context, so it is
    served only to a request whose hidden set is that view's.
    """

    context = context_from_policy_context(policy_context).to_policy_context()
    if (
        not (kind or search or entity or context)
        and runtime._view().hidden == visible_view.view_for(runtime, None).hidden
    ):
        cached = runtime.manifest_catalog(view=view, verbosity=verbosity)
        if cached is not None:
            return cached
    return catalog_payload(
        runtime,
        view=view,
        verbosity=verbosity,
        kind=kind,
        search=search,
        entity=entity,
        policy_context=context,
    )
