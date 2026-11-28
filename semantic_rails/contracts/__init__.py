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

from semantic_rails.errors import SemanticLayerError

from .drift import diff_semantic_contract
from .metric_portability import (
    compare_metric_portability,
    export_metric_portability,
)
from .producer import (
    CONTRACT_FORMAT_VERSION,
    export_semantic_contract,
    semantic_contract_fingerprint,
)

__all__ = [
    "CONTRACT_FORMAT_VERSION",
    "CONTRACT_NAMES",
    "contract_path",
    "compare_metric_portability",
    "diff_semantic_contract",
    "export_metric_portability",
    "load_contract_fixture",
    "export_semantic_contract",
    "load_contract",
    "semantic_contract_fingerprint",
]


CONTRACT_NAMES = (
    "architect_mcp.v2.json",
    "http_api.v1.openapi.json",
    "package.v1.json",
    "metric_portability.v1.json",
    "query.v2.json",
    "query_mcp.v2.json",
    "semantic_contract.v1.json",
    "validation_report.v1.json",
)

# A contract replaced by a new major, and the major that replaced it. Asking for the old
# one is refused by name rather than as an unknown contract.
RETIRED_CONTRACTS = {"architect_mcp.v1.json": "architect_mcp.v2.json"}


def _require_contract_name(name: str) -> None:
    current = RETIRED_CONTRACTS.get(name)
    if current is not None:
        raise SemanticLayerError(
            "INVALID_CONTRACT",
            f"Contract {name!r} (major {_major(name)}) is no longer supported; "
            f"use {current!r} (major {_major(current)}).",
            details={
                "reason": "unsupported_version",
                "requested": name,
                "requested_version": _major(name),
                "current": current,
                "current_version": _major(current),
            },
        )
    if name not in CONTRACT_NAMES:
        raise ValueError(f"Unknown Semantic Rails contract {name!r}")


def _major(name: str) -> str:
    """The ``vN`` major in a contract filename such as ``architect_mcp.v2.json``."""
    return name.removesuffix(".json").rsplit(".", 1)[-1]


def contract_path(name: str) -> Path:
    """Return a filesystem path for a packaged contract artifact.

    ``importlib.resources`` may return a non-``Path`` traversable for zipped
    imports. Semantic Rails wheels are installed unpacked by supported Python
    installers, so converting here gives CLI/build tooling a convenient stable
    path while :func:`load_contract` remains the portable read API.
    """

    _require_contract_name(name)
    return Path(str(files(__package__).joinpath(name)))


def load_contract(name: str) -> dict[str, Any]:
    """Load one canonical contract artifact from the installed distribution."""

    _require_contract_name(name)
    resource = files(__package__).joinpath(name)
    return dict(json.loads(resource.read_text(encoding="utf-8")))


def load_contract_fixture(name: str) -> dict[str, Any]:
    """Load the versioned, engine-owned consumer conformance corpus."""
    if name != "metric_portability.v1.json":
        raise ValueError(f"Unknown Semantic Rails contract fixture {name!r}")
    return dict(
        json.loads(files(__package__).joinpath("fixtures", name).read_text(encoding="utf-8"))
    )
