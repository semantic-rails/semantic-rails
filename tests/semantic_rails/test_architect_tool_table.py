"""The Architect tool table in docs/ARCHITECT_MCP.md is the server's own tool list."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.architect_tool_table as table


def test_the_docs_table_matches_the_server() -> None:
    assert table.main(["--check"]) == 0


def test_a_tool_missing_from_the_docs_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "ARCHITECT_MCP.md"
    text = table.DOC.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| `import_dbt_project` |"))
    doc.write_text(text.replace(row + "\n", ""), encoding="utf-8")
    monkeypatch.setattr(table, "DOC", doc)

    assert table.main(["--check"]) == 1
    assert capsys.readouterr().err.startswith(
        "docs/ARCHITECT_MCP.md's Architect tool table differs from the server's tool list."
    )
    assert table.main([]) == 0
    assert doc.read_text(encoding="utf-8") == text
