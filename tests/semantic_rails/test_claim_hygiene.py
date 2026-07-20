from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _text(*paths: str) -> str:
    return "\n".join((REPO_ROOT / path).read_text(encoding="utf-8") for path in paths)


def test_runtime_docs_do_not_repeat_pre_server_claims():
    text = _text(
        "README.md",
        "docs/MCP_INTERFACE.md",
        "docs/CAPABILITIES.md",
        "docs/PACKAGE_AUTHORING.md",
    )

    assert "does not ship a production MCP server" not in text
    assert "does not define a stdio/SSE/HTTP transport command" not in text
    assert "Snow CLI is the supported connection kind" not in text
    assert "snowflake_native" in text
    assert "semantic-rails mcp stdio" in text
    assert "semantic-rails mcp http" in text
