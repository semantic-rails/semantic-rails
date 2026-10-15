"""Shared, policy-safe catalog resolution for HTTP and MCP transports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import visible_view
from .atomic_files import atomic_write_bytes
from .manifest import DEFAULT_VARIANTS, _engine_version, fingerprint_path, manifest_path
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


def write_manifest(runtime, *, variants: tuple[tuple[str, str], ...] = DEFAULT_VARIANTS) -> Path:
    """Compute catalog payloads for each variant and write to .compiled/.

    Returns the path to the written manifest.json. Overwrites any
    existing manifest atomically. Both compiled artifacts use mode 0644.
    """
    with runtime.request_scope():
        return _write_loaded_manifest(runtime, variants=variants, catalog_payload=catalog_payload)


def _write_loaded_manifest(runtime, *, variants, catalog_payload) -> Path:
    snapshot = runtime.snapshot
    fingerprint = snapshot.source_fingerprint
    catalogs: dict[str, dict[str, Any]] = {}
    for view, verbosity in variants:
        key = f"{view}|{verbosity}"
        catalogs[key] = catalog_payload(
            runtime, view=view, verbosity=verbosity, policy_context=None
        )

    payload = {
        "schema_version": 1,
        "engine_version": _engine_version(),
        "package_id": runtime.package_id,
        "source_path": runtime.source_path,
        "fingerprint": fingerprint,
        "semantic_fingerprint": snapshot.semantic_fingerprint,
        "provenance": dict(snapshot.provenance),
        "source_kind": snapshot.source_kind,
        "variants": [list(v) for v in variants],
        "catalogs": catalogs,
    }

    atomic_write_bytes(
        manifest_path(runtime.source_path), json.dumps(payload, default=str).encode(), mode=0o644
    )
    atomic_write_bytes(fingerprint_path(runtime.source_path), fingerprint.encode(), mode=0o644)
    return manifest_path(runtime.source_path)
