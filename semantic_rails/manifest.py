"""Compiled manifest for a semantic-rails package.

The manifest is a build-time artifact that captures the agent-visible
catalog of a validated package. It exists so that hot read paths
(``catalog`` MCP calls, in particular) do not re-walk the registry
on every request.

Produced by ``semantic-rails validate-config`` (or programmatically via
:func:`write_manifest`). Loaded lazily by :class:`Runtime` on first
catalog access. Keyed by :func:`package_fingerprint` so source edits
invalidate the cached artifact automatically.

Set ``SR_DEV_NO_MANIFEST=1`` to bypass manifest reads (useful when
iterating on a package locally without re-validating after every save).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .cache import package_fingerprint

MANIFEST_DIR = ".compiled"
MANIFEST_FILE = "manifest.json"
FINGERPRINT_FILE = "sources.sha256"

# (view, verbosity) pairs precomputed at manifest write time. These match
# the variants used by ``read_resource`` and ``_handle_catalog`` defaults.
DEFAULT_VARIANTS: tuple[tuple[str, str], ...] = (
    ("summary", "summary"),
    ("summary", "compact"),
    ("summary", "full"),
    ("detailed", "compact"),
    ("detailed", "full"),
)


def manifest_dir(source_path: str) -> Path:
    """Resolve the ``.compiled/`` directory for a package source.

    Directory packages keep it inside the package directory. Single-file
    packages (``--path pkg/package.yml``) get it next to the package
    file — writing under the file path itself would fail, which used to
    leave single-file packages with no manifest at all.
    """
    source = Path(source_path)
    root = source.parent if source.is_file() else source
    return root / MANIFEST_DIR


def manifest_path(source_path: str) -> Path:
    return manifest_dir(source_path) / MANIFEST_FILE


def fingerprint_path(source_path: str) -> Path:
    return manifest_dir(source_path) / FINGERPRINT_FILE


def write_manifest(runtime, *, variants: tuple[tuple[str, str], ...] = DEFAULT_VARIANTS) -> Path:
    """Compute catalog payloads for each variant and write to .compiled/.

    Returns the path to the written manifest.json. Overwrites any
    existing manifest atomically.
    """
    # Local import to avoid a top-level cycle (metadata imports cache).
    from .metadata import catalog_payload

    fingerprint = package_fingerprint(runtime.source_path)
    catalogs: dict[str, dict[str, Any]] = {}
    for view, verbosity in variants:
        key = f"{view}|{verbosity}"
        catalogs[key] = catalog_payload(
            runtime, view=view, verbosity=verbosity, policy_context=None
        )

    payload = {
        "schema_version": 1,
        "package_id": runtime.package_id,
        "source_path": runtime.source_path,
        "fingerprint": fingerprint,
        "variants": [list(v) for v in variants],
        "catalogs": catalogs,
    }

    out_dir = manifest_dir(runtime.source_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / (MANIFEST_FILE + ".tmp")
    tmp.write_text(json.dumps(payload, default=str))
    tmp.replace(manifest_path(runtime.source_path))
    fingerprint_path(runtime.source_path).write_text(fingerprint)
    return manifest_path(runtime.source_path)


def load_manifest(source_path: str) -> dict[str, Any] | None:
    """Load a manifest if present AND its fingerprint matches sources.

    Returns ``None`` if disabled (``SR_DEV_NO_MANIFEST``), missing, or
    stale. Callers should treat ``None`` as "fall back to live compute".

    Each catalog variant is also stored as a pre-encoded JSON string
    under ``catalog_json[key]`` so callers can ``json.loads`` it per
    request without re-encoding. This is the canonical isolation seam:
    every consumer gets a freshly parsed dict tree.
    """
    if os.environ.get("SR_DEV_NO_MANIFEST"):
        return None
    path = manifest_path(source_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if payload.get("schema_version") != 1:
        return None
    current = package_fingerprint(source_path)
    if payload.get("fingerprint") != current:
        return None
    catalogs = payload.get("catalogs", {}) or {}
    payload["catalog_json"] = {
        key: json.dumps(value, default=str) for key, value in catalogs.items()
    }
    return payload


def get_catalog(manifest: dict[str, Any], view: str, verbosity: str) -> dict[str, Any] | None:
    key = f"{view}|{verbosity}"
    return manifest.get("catalogs", {}).get(key)


def get_catalog_json(manifest: dict[str, Any], view: str, verbosity: str) -> str | None:
    """Return the pre-encoded JSON string for a variant, if present."""
    key = f"{view}|{verbosity}"
    return manifest.get("catalog_json", {}).get(key)
