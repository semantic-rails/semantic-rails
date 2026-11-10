"""The docs a new user reads first name what the tools accept."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from semantic_rails.mcp import TOOL_DEFINITIONS

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_docs_call_plans_argument_by_its_name() -> None:
    plan = next(row for row in TOOL_DEFINITIONS if row.name == "plan").to_dict()
    assert "query" in plan["inputSchema"]["properties"]
    assert "partial_query" not in plan["inputSchema"]["properties"]
    stale = [
        path.name
        for path in (REPO_ROOT / "docs").glob("*.md")
        if "partial_query" in path.read_text(encoding="utf-8")
    ]
    assert stale == []


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("catalog", "compact, full"),
        ("discover", "minimal, compact, full"),
        ("inspect", "minimal, compact, full"),
        ("build-options", "compact, full"),
    ],
)
def test_an_unsupported_verbosity_is_refused(command: str, allowed: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "semantic_rails", command, "--verbosity", "summary"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert f"argument --verbosity: invalid choice: 'summary' (choose from {allowed})" in proc.stderr
