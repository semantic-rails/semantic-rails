"""changelog.d/ fragments: the contract `check` enforces and how `release` folds them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import changelog_fragments, verify_release_readiness
from scripts.changelog_fragments import PLACEHOLDER
from scripts.verify_release_readiness import (
    validate_changelog_folded,
    validate_license_posture,
    validate_local_links,
    validate_public_references,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TOP = f"# Changelog\n\n## Unreleased\n\n{PLACEHOLDER}\n\n"
REST = "## 0.1.0 — 2026-06-11 — Initial\n\n### Added\n\n- First release.\n"
CHANGELOG = TOP + REST
OK = ("x.added.md", "- x\n")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    return tmp_path


def _write(root: Path, name: str, body: str | bytes) -> None:
    (root / "changelog.d").mkdir(exist_ok=True)
    (root / "changelog.d" / name).write_bytes(body if isinstance(body, bytes) else body.encode())


def _run(root: Path, command: str, *args: str) -> int:
    return changelog_fragments.main(["--root", str(root), command, *args])


def _release(root: Path, *args: str) -> int:
    return _run(root, "release", "--version", "0.2.0", "--date", "2026-10-01", *args)


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
def test_check_reports_one_message_per_problem(root, capsys, name, body, message):
    _write(root, name, body)
    assert _run(root, "check") == 1
    errors = capsys.readouterr().err.splitlines()
    assert len(errors) == 1 and errors[0].startswith(f"changelog.d/{name}: {message}")


def test_check_preview_and_release_fold_in_order_and_existing_style(root, capsys):
    assert _run(root, "preview") == 0
    assert capsys.readouterr().out == "## Unreleased\n\nNo unreleased changes.\n"
    _write(root, "README.md", "# Ignored even though it has a heading\n")
    _write(root, "tls.security.md", "- Hardened.\n")
    _write(root, "b-flag.added.md", "- Second.\n")
    _write(root, "142-a-flag.added.md", "- First,\n  wrapped.\n  - Nested.\n")
    _write(root, "crash.fixed.md", "- Fixed.\n")
    assert _run(root, "check") == 0
    assert capsys.readouterr().out == "changelog.d/: 4 fragment(s) OK\n"
    body = "### Added\n\n- First,\n  wrapped.\n  - Nested.\n- Second.\n\n### Fixed\n\n- Fixed.\n\n"
    body += "### Security\n\n- Hardened.\n"
    assert _run(root, "preview") == 0
    assert capsys.readouterr().out == f"## Unreleased\n\n{body}"

    # Reproduce the newest real heading, "## X.Y.Z[rcN] — YYYY-MM-DD — Title", byte for byte.
    real = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    version_re = changelog_fragments.VERSION.pattern
    heading = re.search(rf"(?m)^## ({version_re}) — (\d{{4}}-\d{{2}}-\d{{2}}) — (.+)$", real)
    version, day, title = heading.groups()
    assert _run(root, "release", "--version", version, "--date", day, "--title", title) == 0
    folded = f"{TOP}{heading[0]}\n\n{body}\n{REST}"
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == folded
    assert [path.name for path in (root / "changelog.d").iterdir()] == ["README.md"]


@pytest.mark.parametrize(
    ("top", "rest", "args", "heading"),
    [
        (TOP, REST.replace("0.1.0", "0.2.10"), ["--version", "0.2.1"], "## 0.2.1 — 2026-10-01"),
        (TOP, REST.replace("0.1.0", "0.2.1"), ["--version", "0.2.10"], "## 0.2.10 — 2026-10-01"),
        (TOP.rstrip("\n") + "\n", "", [], "## 0.2.0 — 2026-10-01"),  # Unreleased is last
        ("# Changelog\n\n## Unreleased\n", "", [], "## 0.2.0 — 2026-10-01"),  # empty Unreleased
        (TOP, REST, ["--title", "  Theme  "], "## 0.2.0 — 2026-10-01 — Theme"),
        (TOP, REST, ["--title", " "], "## 0.2.0 — 2026-10-01"),
        (TOP, REST, ["--version", "0.2.0rc1"], "## 0.2.0rc1 — 2026-10-01"),
        (TOP, REST.replace("0.1.0", "0.2.0rc1"), [], "## 0.2.0 — 2026-10-01"),  # final after rc
    ],
)
def test_release_folds_edge_cases_exactly(root, top, rest, args, heading):
    (root / "CHANGELOG.md").write_text(top + rest, encoding="utf-8")
    _write(root, *OK)
    assert _release(root, *args) == 0
    folded = f"{TOP}{heading}\n\n### Added\n\n- x\n" + (f"\n{rest}" if rest else "")
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == folded


@pytest.mark.parametrize(
    ("changelog", "fragment", "args", "message"),
    [
        (CHANGELOG, OK, ["--version", "0.1.0"], "already has a ## 0.1.0 section"),
        (CHANGELOG, OK, ["--version", "0.2"], "is not X.Y.Z"),
        (CHANGELOG, OK, ["--version", "0.2.0-rc1"], "is not X.Y.Z"),
        (CHANGELOG, OK, ["--version", "0.2.0.dev1"], "is not X.Y.Z"),
        (CHANGELOG, OK, ["--date", "2026-9-1"], "is not a YYYY-MM-DD date"),
        (CHANGELOG, OK, ["--date", "2026-02-30"], "is not a YYYY-MM-DD date"),
        (CHANGELOG, OK, ["--date", "20261001"], "is not a YYYY-MM-DD date"),
        (CHANGELOG, OK, ["--title", "Theme\n## 9.9.9"], "--title must be a single line"),
        (CHANGELOG, OK, ["--title", "Theme\r"], "--title must be a single line"),
        (CHANGELOG, None, [], "no fragments to fold"),
        (CHANGELOG, ("x.added.md", "Prose.\n"), [], "expected a '- ' bullet"),
        (CHANGELOG.replace("## Unreleased", "## Next"), OK, [], "no ## Unreleased section"),
    ],
)
def test_release_refuses_without_writing(root, capsys, changelog, fragment, args, message):
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    _write(root, "README.md", "# Changelog fragments\n")
    if fragment:
        _write(root, *fragment)
    before = {path.name: path.read_bytes() for path in (root / "changelog.d").iterdir()}
    assert _release(root, *args) == 1
    assert message in capsys.readouterr().err
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == changelog
    assert {path.name: path.read_bytes() for path in (root / "changelog.d").iterdir()} == before


@pytest.mark.parametrize(
    "entry", ["- A.", "* A.", "+ A.", "1. A.", "  - A.", "### Added", "Prose.", "## Unreleased"]
)
def test_check_and_release_allow_only_the_placeholder_under_unreleased(root, capsys, entry):
    changelog = CHANGELOG.replace(PLACEHOLDER, f"{PLACEHOLDER}\n\n{entry}")
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    _write(root, *OK)
    assert _run(root, "check") == 1
    assert _release(root) == 1
    assert capsys.readouterr().err.count("move entries to changelog.d/") == 2
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == changelog
    assert (root / "changelog.d" / "x.added.md").read_bytes() == b"- x\n"


def test_real_changelog_holds_only_the_unreleased_placeholder():
    assert changelog_fragments.read_changelog(REPO_ROOT)[1] == []


def test_tag_gate_needs_the_tagged_heading_and_no_unfolded_fragments(root):
    _write(root, "README.md", "# Changelog fragments\n")
    errors: list[str] = []
    validate_changelog_folded(errors, "v0.1.0", root)
    assert errors == []
    _write(root, *OK)
    validate_changelog_folded(errors, "", root)
    assert errors == []
    validate_changelog_folded(errors, "v0.2.0", root)
    assert len(errors) == 2 and "x.added.md" in errors[0] and "## heading" in errors[1]
    assert _release(root) == 0
    errors.clear()
    validate_changelog_folded(errors, "v0.2.0", root)
    assert errors == []


def test_readiness_scans_pending_fragments_like_the_changelog(root, monkeypatch):
    monkeypatch.setattr(verify_release_readiness, "REPO_ROOT", root)
    (root / "docs").mkdir()
    (root / "docs" / "QUERY_API.md").write_text("# Query API\n", encoding="utf-8")
    _write(root, "README.md", "Folded into [the changelog](../CHANGELOG.md).\n")
    _write(root, "routes.added.md", "- See [the routes](docs/QUERY_API.md#http-routes).\n")
    checks = (validate_local_links, validate_public_references, validate_license_posture)
    errors: list[str] = []
    for check in checks:
        check(errors)
    assert errors == []
    _write(root, "leak.fixed.md", "- [x](docs/GONE.md) semantic-rails-cloud commercial license\n")
    for check in checks:
        check(errors)
    assert errors == [
        "changelog.d/leak.fixed.md has broken local link: docs/GONE.md",
        "changelog.d/leak.fixed.md contains public private repo reference",
        "changelog.d/leak.fixed.md still contains stale license posture wording: commercial license",
    ]
