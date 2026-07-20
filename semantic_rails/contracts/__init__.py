"""Versioned public contracts owned by the Semantic Rails engine.

The JSON artifacts beside this module are the canonical wire contracts for
the public engine.  They are package data, so consumers can inspect them with
``importlib.resources`` after installing the wheel instead of depending on a
source checkout.

The semantic validation contract deliberately stops at the framework-neutral
``semantic`` section.  dbt, SQLMesh, and future integrations own their
``binding`` sections and compose their schemas with
``semantic_contract.v1.json``.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from .producer import (
    CONTRACT_FORMAT_VERSION,
    export_semantic_contract,
    semantic_contract_fingerprint,
)

__all__ = [
    "CONTRACT_FORMAT_VERSION",
    "CONTRACT_NAMES",
    "contract_path",
    "export_semantic_contract",
    "load_contract",
    "semantic_contract_fingerprint",
]


CONTRACT_NAMES = (
    "architect_mcp.v1.json",
    "http_api.v1.openapi.json",
    "package.v1.json",
    "query_ir.preview.v2.json",
    "query_ir.v1.json",
    "query_mcp.v1.json",
    "semantic_contract.v1.json",
    "validation_report.v1.json",
)


def contract_path(name: str) -> Path:
    """Return a filesystem path for a packaged contract artifact.

    ``importlib.resources`` may return a non-``Path`` traversable for zipped
    imports. Semantic Rails wheels are installed unpacked by supported Python
    installers, so converting here gives CLI/build tooling a convenient stable
    path while :func:`load_contract` remains the portable read API.
    """

    if name not in CONTRACT_NAMES:
        raise ValueError(f"Unknown Semantic Rails contract {name!r}")
    return Path(str(files(__package__).joinpath(name)))


def load_contract(name: str) -> dict[str, Any]:
    """Load one canonical contract artifact from the installed distribution."""

    if name not in CONTRACT_NAMES:
        raise ValueError(f"Unknown Semantic Rails contract {name!r}")
    resource = files(__package__).joinpath(name)
    return dict(json.loads(resource.read_text(encoding="utf-8")))
