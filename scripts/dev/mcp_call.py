"""Call an MCP tool against the bundled jaffle_shop demo, print the envelope.

Usage:
    uv run python scripts/dev/mcp_call.py <tool> '<json-arguments>'
    uv run python scripts/dev/mcp_call.py list-tools

Exercises the exact in-process adapter (`SemanticLayerMCPAdapter`) the
MCP transports wrap, so the printed envelope matches what an MCP client
receives. The demo package is copied (with a seeded warehouse) into a
stable temp location on first use and reused afterwards.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from semantic_rails import config as config_module  # noqa: E402
from semantic_rails.mcp import SemanticLayerMCPAdapter  # noqa: E402
from semantic_rails.runtime import Runtime  # noqa: E402
from tests.semantic_rails.conftest import copy_package_config  # noqa: E402

_WORKDIR = Path(tempfile.gettempdir()) / "semantic_rails_mcp_call"


def _package_path() -> Path:
    marker = _WORKDIR / "package_path.txt"
    if marker.exists():
        existing = Path(marker.read_text(encoding="utf-8").strip())
        if existing.exists():
            return existing
    _WORKDIR.mkdir(parents=True, exist_ok=True)
    package_path = copy_package_config(_WORKDIR, "jaffle_shop", preseed_db=True)
    marker.write_text(str(package_path), encoding="utf-8")
    return package_path


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    package_path = _package_path()
    config_module.list_package_paths = lambda: {"jaffle_shop": str(package_path)}
    adapter = SemanticLayerMCPAdapter(Runtime("jaffle_shop"))
    tool = sys.argv[1]
    if tool == "list-tools":
        for definition in adapter.list_tools():
            print(f"{definition['name']}: {definition['description'][:120]}")
        return 0
    arguments = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    result = adapter.call_tool(tool, arguments)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
