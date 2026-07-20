"""GET /api/v1/capabilities must ship the documented ``expression_shapes``.

docs/QUERY_API.md promises a structured list of the accepted
``select[*].expression`` shapes — ``{name, description, example}`` dicts —
so agents can introspect the IR contract at runtime without parsing
``schemas/query_ir.v1.json`` out of band. The field existed on the MCP
capabilities tool (``metadata_parts/capabilities.py``) but the HTTP route
never returned it. Regression coverage for that gap, plus drift guards:
the shipped names must match both the parser's expression-kind registry
and the kinds enumerated in the doc.
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
        service = SemanticHTTPService(runtime)
        payload, status = service.handle("GET", "/capabilities")
        assert status == 200
        shapes = payload.get("expression_shapes")
        assert isinstance(shapes, list) and shapes, (
            "GET /api/v1/capabilities must return the documented "
            f"expression_shapes list; got {shapes!r}"
        )
        for shape in shapes:
            assert isinstance(shape, dict), shape
            assert set(shape) == {"name", "description", "example"}, (
                f"each expression shape must be a {{name, description, example}} "
                f"dict per docs/QUERY_API.md; got keys {sorted(shape)}"
            )
            assert shape["name"] and shape["description"], shape
            assert isinstance(shape["example"], dict) and shape["example"], shape
        names = {str(shape["name"]) for shape in shapes}
        assert names == _documented_shape_names(), (
            "expression_shapes names drifted from the docs/QUERY_API.md "
            f"enumeration; payload={sorted(names)}"
        )
    finally:
        runtime.close()


def test_http_expression_shape_names_are_parser_kinds(runtime_factory):
    """Every advertised shape name must be a kind the expression parser
    actually accepts — content derives from the supported kinds, not from
    strings that can drift."""
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = SemanticHTTPService(runtime).capabilities_payload()
        for shape in payload["expression_shapes"]:
            name = str(shape["name"])
            assert name in _VALID_KEYS_BY_KIND, (
                f"expression shape {name!r} is not a parser-accepted kind; "
                "update metadata_parts/capabilities._EXPRESSION_SHAPES"
            )
    finally:
        runtime.close()


def test_http_and_mcp_capabilities_share_expression_shapes(runtime_factory):
    """The HTTP route and the MCP capabilities tool must ship the same
    expression_shapes — one registry, no per-surface drift."""
    runtime = runtime_factory("jaffle_shop")
    try:
        http_shapes = SemanticHTTPService(runtime).capabilities_payload()["expression_shapes"]
        mcp_shapes = mcp_capabilities_payload(runtime)["expression_shapes"]
        assert http_shapes == mcp_shapes
    finally:
        runtime.close()


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
