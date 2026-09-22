"""changelog.d/ fragments: the contract `check` enforces and how `release` folds them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import changelog_fragments
from scripts.verify_release_readiness import validate_changelog_folded

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = (
    "# Changelog\n\n## Unreleased\n\nPending changes live in `changelog.d/`.\n\n"
    "## 0.1.0 — 2026-06-11 — Initial\n\n### Added\n\n- First release.\n"
)
OK = ("x.added.md", "- x\n")


def _write(root: Path, name: str, body: str | bytes) -> None:
    (root / "changelog.d").mkdir(exist_ok=True)
    (root / "changelog.d" / name).write_bytes(body if isinstance(body, bytes) else body.encode())


def _run(root: Path, command: str, *args: str) -> int:
    return changelog_fragments.main(["--root", str(root), command, *args])


@pytest.mark.parametrize(
    ("name", "body", "message"),
    [
        ("notes.md", "- x\n", "name must be <slug>.<category>.md"),
        ("Big_Change.added.md", "- x\n", "slug must be lowercase kebab-case"),
        ("x.improved.md", "- x\n", "category must be one of added, changed,"),
        ("x.fixed.md", b"- caf\xe9\n", "is not valid UTF-8"),
        ("x.fixed.md", " \n", "is empty"),
        ("x.fixed.md", "- x", "must end with a newline"),
        ("x.fixed.md", "### Fixed\n- x\n", "line 1: headings are not allowed"),
        ("x.fixed.md", "Prose.\n", "line 1: expected a '- ' bullet"),
        ("x.fixed.md", "- x\n\n- y\n", "line 2: expected a '- ' bullet"),
    ],
)
def test_check_reports_one_message_per_problem(tmp_path, capsys, name, body, message):
    _write(tmp_path, name, body)
    assert _run(tmp_path, "check") == 1
    errors = capsys.readouterr().err.splitlines()
    assert len(errors) == 1 and errors[0].startswith(f"changelog.d/{name}: {message}")


def test_check_preview_and_release_fold_in_order_and_existing_style(tmp_path, capsys):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    assert _run(tmp_path, "preview") == 0
    assert capsys.readouterr().out == "## Unreleased\n\nNo unreleased changes.\n"
    _write(tmp_path, "README.md", "# Ignored even though it has a heading\n")
    _write(tmp_path, "tls.security.md", "- Hardened.\n")
    _write(tmp_path, "b-flag.added.md", "- Second.\n")
    _write(tmp_path, "142-a-flag.added.md", "- First,\n  wrapped.\n  - Nested.\n")
    _write(tmp_path, "crash.fixed.md", "- Fixed.\n")
    assert _run(tmp_path, "check") == 0
    assert capsys.readouterr().out == "changelog.d/: 4 fragment(s) OK\n"
    body = "### Added\n\n- First,\n  wrapped.\n  - Nested.\n- Second.\n\n### Fixed\n\n- Fixed.\n\n"
    body += "### Security\n\n- Hardened.\n"
    assert _run(tmp_path, "preview") == 0
    assert capsys.readouterr().out == f"## Unreleased\n\n{body}"

    # Reproduce the newest real heading, "## X.Y.Z — YYYY-MM-DD — Title", byte for byte.
    real = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.search(r"(?m)^## (\d+\.\d+\.\d+) — (\d{4}-\d{2}-\d{2}) — (.+)$", real)
    version, day, title = heading.groups()
    assert _run(tmp_path, "release", "--version", version, "--date", day, "--title", title) == 0
    folded = CHANGELOG.replace("## 0.1.0", f"{heading[0]}\n\n{body}\n## 0.1.0")
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == folded
    assert [path.name for path in (tmp_path / "changelog.d").iterdir()] == ["README.md"]


@pytest.mark.parametrize(
    ("changelog", "fragment", "args", "message"),
    [
        (CHANGELOG, OK, ["--version", "0.1.0"], "already has a ## 0.1.0 section"),
        (CHANGELOG, OK, ["--version", "0.2"], "is not X.Y.Z"),
        (CHANGELOG, OK, ["--date", "2026-9-1"], "is not a YYYY-MM-DD date"),
        (CHANGELOG, OK, ["--date", "2026-02-30"], "is not a YYYY-MM-DD date"),
        (CHANGELOG, None, [], "no fragments to fold"),
        (CHANGELOG, ("x.added.md", "Prose.\n"), [], "expected a '- ' bullet"),
        (CHANGELOG.replace("Pending", "- Hand-written.\n\nPending"), OK, [], "entries under"),
        (CHANGELOG.replace("## Unreleased", "## Next"), OK, [], "no ## Unreleased section"),
    ],
)
def test_release_refuses_without_writing(tmp_path, capsys, changelog, fragment, args, message):
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    _write(tmp_path, "README.md", "# Changelog fragments\n")
    if fragment:
        _write(tmp_path, *fragment)
    before = sorted((tmp_path / "changelog.d").iterdir())
    assert _run(tmp_path, "release", "--version", "0.2.0", "--date", "2026-10-01", *args) == 1
    assert message in capsys.readouterr().err
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == changelog
    assert sorted((tmp_path / "changelog.d").iterdir()) == before


def test_tag_gate_rejects_unfolded_fragments_until_release_folds_them(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    _write(tmp_path, "README.md", "# Changelog fragments\n")
    _write(tmp_path, *OK)
    errors: list[str] = []
    validate_changelog_folded(errors, "", tmp_path)
    assert errors == []
    validate_changelog_folded(errors, "v0.2.0", tmp_path)
    assert len(errors) == 1 and "x.added.md" in errors[0]
    assert _run(tmp_path, "release", "--version", "0.2.0", "--date", "2026-10-01") == 0
    changelog = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "`.\n\n## 0.2.0 — 2026-10-01\n\n### Added\n\n- x\n\n## 0.1.0 —" in changelog
    errors.clear()
    validate_changelog_folded(errors, "v0.2.0", tmp_path)
    assert errors == []
