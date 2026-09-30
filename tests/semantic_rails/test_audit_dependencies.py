"""Offline policy and command-boundary coverage for dependency audits."""

from __future__ import annotations

import io
import json
import subprocess
import tomllib
from dataclasses import replace
from datetime import date, timedelta

import pytest

from scripts import audit_dependencies as audit

TODAY = date(2026, 9, 30)
ENTRY = audit.ExceptionEntry(
    id="CVE-2026-49265",
    package="oauthlib",
    extras=["databricks"],
    blocked_by="databricks-sql-connector: oauthlib<4.0.0",
    fixed_in="4.0.0",
    issue="https://github.com/semantic-rails/semantic-rails/issues/210",
    review_by=TODAY + timedelta(days=30),
    reason="Connector caps the fixed release.",
)
EXCEPTION_TOML = f"""[[exceptions]]
id = {json.dumps(ENTRY.id)}
package = {json.dumps(ENTRY.package)}
extras = {json.dumps(ENTRY.extras)}
blocked_by = {json.dumps(ENTRY.blocked_by)}
fixed_in = {json.dumps(ENTRY.fixed_in)}
issue = {json.dumps(ENTRY.issue)}
review_by = {ENTRY.review_by.isoformat()}
reason = {json.dumps(ENTRY.reason)}
"""


def dependency(package="oauthlib", vulnerable=True):
    return {
        "name": package,
        "version": "3.3.1",
        "vulns": [{"id": "GHSA-example", "aliases": [ENTRY.id], "fix_versions": ["4.0.0"]}]
        if vulnerable
        else [],
    }


@pytest.fixture
def reports():
    result = {surface: [] for surface in audit.audit_surfaces(audit.ROOT)}
    result["databricks"] = [dependency()]
    return result


@pytest.mark.parametrize(
    "case, expected",
    [
        ("unlisted", "unexcepted advisory"),
        ("core", "reachable from core"),
        ("core-clean", "reachable from core"),
        ("expired", "expired review_by"),
        ("resolvable", "upgrade now"),
        ("unused", "unused exception"),
        ("wrong-package", "unexcepted advisory"),
        ("wrong-extra", "unexcepted advisory"),
        ("extra-unused", "unused exception for snowflake"),
        ("happy", None),
        ("review-today", None),
    ],
)
def test_policy(reports, case, expected):
    entries = [ENTRY]
    if case == "unlisted":
        entries = []
    elif case in ("core", "core-clean"):
        reports["core"] = [dependency(vulnerable=case == "core")]
    elif case == "expired":
        entries = [replace(ENTRY, review_by=TODAY - timedelta(days=1))]
    elif case == "review-today":
        entries = [replace(ENTRY, review_by=TODAY)]
    elif case == "unused":
        reports["databricks"] = [dependency(vulnerable=False)]
    elif case == "wrong-package":
        entries = [replace(ENTRY, package="other")]
    elif case == "wrong-extra":
        reports["snowflake"] = [dependency()]
    elif case == "extra-unused":
        entries = [replace(ENTRY, extras=["databricks", "snowflake"])]
    lines, errors = audit.check_policy(reports, entries, TODAY, lambda *_: case != "resolvable")
    assert len(lines) == len(reports)
    if expected:
        assert any(expected in error for error in errors)
    else:
        assert errors == []
        assert any("[databricks]" in line and "listed exception" in line for line in lines)


def test_normalized_package_name(reports):
    reports["databricks"] = [dependency(package="OAuth_Lib")]
    _, errors = audit.check_policy(
        reports,
        [replace(ENTRY, package="oauth-lib")],
        TODAY,
        lambda *_: True,
    )
    assert errors == []


@pytest.mark.parametrize(
    "edit",
    [
        'extras = ["core"]',
        "extras = []",
        'extras = ["databricks", "databricks"]',
        "review_by = 2026-10-31",
        'review_by = "2026-10-30"',
        'reason = ""',
        'issue = "placeholder"',
        'package = "oauthlib>=1"',
        'fixed_in = "4.0.0; other"',
        'blocked_by = "unstructured text"',
    ],
)
def test_invalid_exception_file(tmp_path, edit):
    source = EXCEPTION_TOML
    field = edit.split(" = ")[0]
    lines = [edit if line.startswith(field + " = ") else line for line in source.splitlines()]
    path = tmp_path / "exceptions.toml"
    path.write_text("\n".join(lines))
    with pytest.raises(ValueError):
        audit.load_exceptions(path, TODAY)


def test_exception_file_valid_and_duplicate_rejected(tmp_path):
    path = tmp_path / "exceptions.toml"
    path.write_text(EXCEPTION_TOML)
    assert audit.load_exceptions(path, TODAY) == [ENTRY]
    duplicate = tmp_path / "duplicate.toml"
    duplicate.write_text(path.read_text() * 2)
    with pytest.raises(ValueError, match="duplicate exception"):
        audit.load_exceptions(duplicate, TODAY)


def test_live_exception_file_schema():
    audit.load_exceptions(audit.ROOT / "security/audit-exceptions.toml", date.today())


def test_every_published_extra_is_audited(tmp_path, monkeypatch):
    # Also exercise discovery of an extra added after this test was written.
    project = tomllib.loads((audit.ROOT / "pyproject.toml").read_text())["project"]
    extras = [*project["optional-dependencies"], "future-extra"]
    (tmp_path / "pyproject.toml").write_text(
        "[project.optional-dependencies]\n" + "\n".join(f"{extra} = []" for extra in extras)
    )
    audited = []

    def audit_surface(root, surface, scratch):
        audited.append(surface)
        return [dependency(vulnerable=False)]

    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(audit, "load_exceptions", lambda *_: [])
    monkeypatch.setattr(audit, "audit_surface", audit_surface)
    assert audit.main() == 0
    assert set(audited) == {"core", *extras} - {"all"}


@pytest.mark.parametrize("surface", ["repl", "server"])
def test_nonconnector_advisory_cannot_be_excepted(reports, surface):
    reports["databricks"] = []
    reports[surface] = [dependency()]
    _, errors = audit.check_policy(
        reports, [replace(ENTRY, extras=[surface])], TODAY, lambda *_: True
    )
    assert any(f"[{surface}]" in error and "unexcepted advisory" in error for error in errors)


def test_all_advisory_patched_versions_are_checked(reports):
    reports["databricks"][0]["vulns"][0]["fix_versions"] = ["4.0.0", "3.3.2"]
    checked = []

    def cap_excludes(extra, entry, versions):
        assert extra == "databricks" and entry == ENTRY
        checked.extend(versions)
        return True

    _, errors = audit.check_policy(reports, [ENTRY], TODAY, cap_excludes)
    assert errors == []
    assert checked == ["4.0.0", "3.3.2"]


@pytest.mark.parametrize("fixes", [None, [], ["3.3.2"], "4.0.0", ["4.0.0", None]])
def test_fixed_in_must_match_advisory_evidence(reports, fixes):
    reports["databricks"][0]["vulns"][0]["fix_versions"] = fixes

    def cap_excludes(*_):
        pytest.fail("Invalid patched-version evidence must fail before metadata verification")

    _, errors = audit.check_policy(reports, [ENTRY], TODAY, cap_excludes)
    assert any("fixed_in missing from advisory patched versions" in error for error in errors)


@pytest.mark.parametrize("source", ["", "[exceptions]\n", "exceptions = []\ntypo = []\n"])
def test_invalid_exception_document(tmp_path, source):
    path = tmp_path / "exceptions.toml"
    path.write_text(source)
    with pytest.raises(ValueError, match="exceptions array"):
        audit.load_exceptions(path, TODAY)


def test_empty_exception_list_supported(tmp_path):
    path = tmp_path / "exceptions.toml"
    path.write_text("exceptions = []\n")
    assert audit.load_exceptions(path, TODAY) == []


@pytest.mark.parametrize("surface", audit.audit_surfaces(audit.ROOT))
def test_audit_exports_only_selected_locked_surface(tmp_path, monkeypatch, surface):
    calls = []

    def run(command, root):
        calls.append(command)
        if command[1] == "export":
            return subprocess.CompletedProcess(command, 0, "oauthlib==3.3.1\n", "")
        return subprocess.CompletedProcess(
            command, 1, json.dumps({"dependencies": [dependency()]}), ""
        )

    monkeypatch.setattr(audit, "run", run)
    assert audit.audit_surface(tmp_path, surface, tmp_path) == [dependency()]
    assert "--locked" in calls[0] and "--no-dev" in calls[0]
    assert "--all-extras" not in calls[0]
    assert ("--extra" in calls[0]) == (surface != "core")
    if surface != "core":
        assert calls[0][-2:] == ["--extra", surface]
    assert "--strict" in calls[1] and "--disable-pip" in calls[1]


@pytest.mark.parametrize(
    "code, payload",
    [
        (2, {}),
        (1, {"dependencies": [dependency(vulnerable=False)]}),
        (0, {"dependencies": []}),
        (0, {"dependencies": [{"name": "oauthlib", "skip_reason": "lookup failed"}]}),
    ],
)
def test_failed_or_partial_audit_rejected(tmp_path, monkeypatch, code, payload):
    def run(command, root):
        if command[1] == "export":
            return subprocess.CompletedProcess(command, 0, "oauthlib==3.3.1\n", "")
        return subprocess.CompletedProcess(command, code, json.dumps(payload), "error")

    monkeypatch.setattr(audit, "run", run)
    with pytest.raises(ValueError):
        audit.audit_surface(tmp_path, "core", tmp_path)


@pytest.fixture
def metadata_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["duckdb>=1.5.6"]\n'
        '[project.optional-dependencies]\ndatabricks = ["databricks-sql-connector[auth]>=4.3.0"]\n'
    )
    return tmp_path


def metadata(requirements):
    return {
        "info": {
            "name": "databricks-sql-connector",
            "version": "4.3.0",
            "requires_dist": requirements,
        }
    }


def mock_metadata(monkeypatch, document):
    def urlopen(url, *, timeout):
        assert url == "https://pypi.org/pypi/databricks-sql-connector/json"
        assert timeout == 30
        return io.BytesIO(json.dumps(document).encode())

    monkeypatch.setattr(audit, "urlopen", urlopen)


@pytest.mark.parametrize(
    "requirements, fixes, expected",
    [
        (["oauthlib>=3.1,<4"], ["4.0.0"], True),
        (["oauthlib>=3.1,<4"], ["4.0.0", "3.3.2"], False),
        (["oauthlib>=3.1,<3.3.2"], ["4.0.0", "3.3.2"], True),
        (["oauthlib>=3.1,<5"], ["4.0.0"], False),
        (["oauthlib"], ["4.0.0"], False),
        (["oauthlib<4; extra == 'auth'"], ["4.0.0"], True),
        (["oauthlib<4; python_version >= '3.11'"], ["4.0.0"], True),
        (["oauthlib<4; extra == 'unused'"], ["4.0.0"], None),
        (["oauthlib<4; extra == 'databricks'"], ["4.0.0"], None),
        (["oauthlib<4; python_version < '3.0'"], ["4.0.0"], None),
        (["oauthlib>=4", "oauthlib<4; extra == 'unused'"], ["4.0.0"], False),
        (["other<4"], ["4.0.0"], None),
        (["oauthlib-tools<4"], ["4.0.0"], None),
        (["oauthlib @ https://example.com/oauthlib.whl"], ["4.0.0"], None),
        (["oauthlib<4", "not a requirement!"], ["4.0.0"], None),
        ([None], ["4.0.0"], None),
        (None, ["4.0.0"], None),
        ([], ["4.0.0"], None),
        (["oauthlib<4"], ["4.0.0", "invalid-version"], None),
    ],
)
def test_latest_metadata_cap(metadata_project, monkeypatch, reports, requirements, fixes, expected):
    mock_metadata(monkeypatch, metadata(requirements))
    reports["databricks"][0]["vulns"][0]["fix_versions"] = fixes

    def check():
        return audit.check_policy(
            reports,
            [ENTRY],
            TODAY,
            lambda extra, entry, versions: audit.cap_excludes_fixes(
                metadata_project, extra, entry, versions
            ),
        )

    if expected is None:
        with pytest.raises(ValueError, match="cannot verify the cap"):
            check()
    else:
        _, errors = check()
        if expected:
            assert errors == []
        else:
            assert any("cap lifted: upgrade now" in error for error in errors)


@pytest.mark.parametrize("document", [{}, {"info": {}}, {"info": None}])
def test_invalid_metadata_document(metadata_project, monkeypatch, document):
    mock_metadata(monkeypatch, document)
    with pytest.raises(ValueError, match="cannot verify the cap"):
        audit.cap_excludes_fixes(metadata_project, "databricks", ENTRY, ["4.0.0"])


@pytest.mark.parametrize("error", [OSError("missing package"), TimeoutError("timeout")])
def test_metadata_fetch_errors_fail_closed(metadata_project, monkeypatch, error):
    def urlopen(*args, **kwargs):
        raise error

    monkeypatch.setattr(audit, "urlopen", urlopen)
    with pytest.raises(ValueError, match="cannot verify the cap"):
        audit.cap_excludes_fixes(metadata_project, "databricks", ENTRY, ["4.0.0"])


def test_malformed_json_fails_closed(metadata_project, monkeypatch):
    monkeypatch.setattr(audit, "urlopen", lambda *a, **kw: io.BytesIO(b"not JSON"))
    with pytest.raises(ValueError, match="cannot verify the cap"):
        audit.cap_excludes_fixes(metadata_project, "databricks", ENTRY, ["4.0.0"])


@pytest.mark.parametrize(
    "requirements",
    [
        ["duckdb<1.5.6"],  # A conflict involving the blocker says nothing about oauthlib.
        None,  # A missing package has no verifiable dependency metadata.
    ],
    ids=["unrelated-duckdb-conflict", "missing-package"],
)
def test_unrelated_failures_cannot_authorize_exception(
    metadata_project, monkeypatch, reports, capsys, requirements
):
    mock_metadata(monkeypatch, metadata(requirements))
    monkeypatch.setattr(audit, "ROOT", metadata_project)
    monkeypatch.setattr(audit, "load_exceptions", lambda *_: [ENTRY])
    monkeypatch.setattr(audit, "audit_surface", lambda root, surface, scratch: reports[surface])
    assert audit.main() == 1
    assert "cannot verify the cap" in capsys.readouterr().err


@pytest.mark.parametrize(
    "aggregate",
    [
        ["foo>=1", "other>=2", "all-only==1"],
        ["foo>=1"],
        ["foo>=1", "other>=3"],
    ],
)
def test_all_must_equal_other_extras_union(tmp_path, monkeypatch, capsys, aggregate):
    (tmp_path / "pyproject.toml").write_text(
        '[project.optional-dependencies]\nfirst = ["foo>=1"]\nsecond = ["other>=2"]\n'
        f"all = {json.dumps(aggregate)}\n"
    )
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(audit, "load_exceptions", lambda *_: [])
    monkeypatch.setattr(
        audit, "audit_surface", lambda *_: pytest.fail("must reject before auditing")
    )
    assert audit.main() == 1
    assert "all must equal the union of the other extras" in capsys.readouterr().err


def test_all_union_normalizes_requirements(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project.optional-dependencies]\nfirst = ["Some_Pkg[AUTH_EXTRA]>=1,<2"]\n'
        'second = ["other>=2"]\nall = ["some-pkg[auth-extra]<2,>=1", "other >= 2"]\n'
    )
    assert audit.audit_surfaces(tmp_path) == ("core", "first", "second")


def test_main_fails_on_timeout(monkeypatch):
    def audit_surface(*_):
        raise subprocess.TimeoutExpired("pip-audit", 120)

    monkeypatch.setattr(audit, "audit_surface", audit_surface)
    monkeypatch.setattr(audit, "load_exceptions", lambda *_: [ENTRY])
    assert audit.main() == 1


def test_external_commands_have_timeout(monkeypatch):
    def run(command, **kwargs):
        assert kwargs["timeout"] == 120
        assert kwargs["capture_output"] is True
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert audit.run(["uv", "export"], audit.ROOT).returncode == 0
