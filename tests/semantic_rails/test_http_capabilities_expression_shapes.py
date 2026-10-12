"""GET /api/v1/capabilities ships the documented ``expression_shapes``.

The names match the parser's kinds, the docs/QUERY_API.md enumeration and the MCP
capabilities tool: one registry, no per-surface drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from semantic_rails.expressions import _VALID_KEYS_BY_KIND
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.metadata import catalog_payload
from semantic_rails.metadata_parts.capabilities import (
    capabilities_payload as mcp_capabilities_payload,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
QUERY_API_DOC = REPO_ROOT / "docs" / "QUERY_API.md"


def _documented_shape_names() -> set[str]:
    """Extract the backticked kind list from the docs/QUERY_API.md sentence
    that documents ``expression_shapes`` (the parenthesized enumeration)."""
    text = QUERY_API_DOC.read_text(encoding="utf-8")
    match = re.search(r"`expression_shapes`[^(]*\(([^)]*)\)", text)
    assert match, "docs/QUERY_API.md no longer documents expression_shapes"
    names = set(re.findall(r"`([a-z_]+)`", match.group(1)))
    assert names, "could not parse documented expression shape names"
    return names


def test_http_capabilities_returns_documented_expression_shapes(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload, status = SemanticHTTPService(runtime).handle("GET", "/capabilities")
        mcp_shapes = mcp_capabilities_payload(runtime)["expression_shapes"]
    finally:
        runtime.close()
    assert status == 200
    shapes = payload["expression_shapes"]
    assert shapes and shapes == mcp_shapes
    for shape in shapes:
        assert set(shape) == {"name", "description", "example"}, shape
        assert shape["name"] and shape["description"] and shape["example"], shape
        assert isinstance(shape["example"], (dict, str)), shape
        assert shape["name"] in _VALID_KEYS_BY_KIND, shape["name"]
    assert {shape["name"] for shape in shapes} == _documented_shape_names()


def test_http_capabilities_uses_precomputed_catalog_service_manifest(
    runtime_factory, monkeypatch
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        manifest_catalog = catalog_payload(runtime, view="summary", verbosity="compact")
        hits: list[tuple[str, str]] = []

        def manifest(*, view: str, verbosity: str):
            hits.append((view, verbosity))
            return manifest_catalog

        monkeypatch.setattr(runtime, "manifest_catalog", manifest)
        monkeypatch.setattr(
            runtime,
            "catalog",
            lambda: pytest.fail("capabilities bypassed the shared manifest-aware catalog service"),
        )

        payload = SemanticHTTPService(runtime).capabilities_payload()

        assert payload["ok"] is True
        assert hits == [("summary", "compact")]
    finally:
        runtime.close()
