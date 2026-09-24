"""base_ref comparisons resolve in the package's own git repository."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.config_validation import PackageReference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_tools import (
    _extract_package_from_git,
    check_package_report,
    diff_package_report,
    impact_report,
    promote_package_report,
)
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse, write_orders_package

MARGIN = {
    "label": "Average order",
    "description": "Order total per order.",
    "kind": "aggregate",
    "measure": "order_total",
    "value_type": "currency",
}


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, input=stdin, env=env
    ).stdout.decode()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A git repository holding a dbt-shaped package under semantic/shop, one commit in."""
    root = tmp_path / "analytics"
    package = write_orders_package(root / "semantic", seed={"kind": "external"})
    build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    (root / ".gitignore").write_text("*.duckdb\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "Orders package")
    return root


def _add_metric(package: Path) -> None:
    path = package / "metrics" / "core.yml"
    metrics = yaml.safe_load(path.read_text(encoding="utf-8"))
    metrics["metrics"]["average_order"] = MARGIN
    path.write_text(yaml.safe_dump(metrics, sort_keys=False), encoding="utf-8")


def test_impact_against_a_ref_in_the_packages_own_repository(repo: Path) -> None:
    package = repo / "semantic" / "shop"
    _add_metric(package)

    report = impact_report(PackageReference(source_path=str(package)), base_ref="HEAD")

    assert report["ok"] is True
    changes = {(row["object_id"], row["change_type"]) for row in report["changes"]}
    assert changes == {("metric.shop.average_order", "added")}
    assert report["comparison"]["label"] == "HEAD"
    assert report["comparison"]["source_path"].startswith("HEAD@")
    assert report["comparison"]["source_path"].endswith(":semantic/shop")


def test_mcp_session_compares_against_the_previous_commit(repo: Path) -> None:
    package = repo / "semantic" / "shop"
    _add_metric(package)
    _git(repo, "commit", "-q", "-am", "Average order")
    server = create_architect_mcp_server(workspace_root=repo)

    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:
            results = []
            for name in ("diff_project", "impact_project", "promotion_check"):
                arguments = {"project_path": "semantic/shop", "base_ref": "HEAD~1"}
                if name == "promotion_check":
                    arguments["environment"] = "staging"
                result = await session.call_tool(name, arguments)
                results.append(dict(result.structuredContent or {}))
            return results

    diff, impact, promotion = asyncio.run(run())

    assert diff["ok"] is True, diff
    assert [row["object_id"] for row in diff["changes"]] == ["metric.shop.average_order"]
    assert impact["ok"] is True, impact
    assert impact["impact"]["risk"] == "high"
    assert promotion["ok"] is True, promotion
    assert promotion["artifacts"]["impact"]["summary"]["changes_total"] == 1


def test_cli_diff_impact_and_promotion_use_the_packages_repository(repo: Path) -> None:
    package = repo / "semantic" / "shop"
    _add_metric(package)
    for command in ("diff-package", "impact-report", "promote-package"):
        arguments = [
            sys.executable,
            "-m",
            "semantic_rails",
            command,
            "--path",
            str(package),
            "--base-ref",
            "HEAD",
        ]
        if command == "promote-package":
            arguments.extend(("--environment", "staging"))
        result = subprocess.run(arguments, capture_output=True, text=True, check=True)
        report = json.loads(result.stdout)
        assert report["ok"] is True, report
        compared = report["artifacts"]["impact"] if command == "promote-package" else report
        assert compared["comparison"]["source_path"].endswith(":semantic/shop")
        assert compared["summary"]["changes_total"] == 1


def test_cli_and_mcp_report_a_rejected_baseline_instead_of_low_risk(repo: Path) -> None:
    package = repo / "semantic" / "shop"
    shared = package / "shared" / "extra.yml"
    shared.parent.mkdir()
    shared.write_text(yaml.safe_dump({"metrics": {"average_order": MARGIN}}))
    link = package / "metrics" / "extra.yml"
    link.symlink_to("../shared/extra.yml")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Symlinked metric")
    link.unlink()

    for command in ("diff-package", "impact-report", "check", "promote-package"):
        arguments = [
            sys.executable,
            "-m",
            "semantic_rails",
            command,
            "--path",
            str(package),
            "--base-ref",
            "HEAD",
        ]
        if command == "promote-package":
            arguments.extend(("--environment", "staging"))
        result = subprocess.run(arguments, capture_output=True, text=True)
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "INVALID_CONFIG"
        assert "cannot be compared" in payload["error"]["message"]

    server = create_architect_mcp_server(workspace_root=repo)

    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:
            results = []
            for name in ("diff_project", "impact_project", "promotion_check"):
                arguments = {"project_path": "semantic/shop", "base_ref": "HEAD"}
                if name == "promotion_check":
                    arguments["environment"] = "staging"
                result = await session.call_tool(name, arguments)
                results.append(dict(result.structuredContent or {}))
            return results

    for payload in asyncio.run(run()):
        assert payload["ok"] is False
        assert payload["error"]["code"] == "INVALID_CONFIG"
        assert "cannot be compared" in payload["error"]["message"]


def test_a_package_at_the_repository_root(tmp_path: Path) -> None:
    package = write_orders_package(tmp_path)
    _git(package, "init", "-q", "-b", "main")
    _git(package, "add", "-A")
    _git(package, "commit", "-q", "-m", "Package")
    _add_metric(package)

    report = diff_package_report(PackageReference(source_path=str(package)), base_ref="main")

    assert [row["object_id"] for row in report["changes"]] == ["metric.shop.average_order"]
    assert report["comparison"]["source_path"].endswith(":.")


@pytest.mark.parametrize(
    ("base_ref", "code", "message"),
    [
        ("--output=/tmp/x", "INVALID_CONFIG", "is not a git revision"),
        ("", "INVALID_CONFIG", "compare_path or base_ref"),
        ("no-such-branch", "OBJECT_NOT_FOUND", "does not name a commit"),
        ("HEAD:semantic", "OBJECT_NOT_FOUND", "does not name a commit"),
        ("HEAD\x00", "INVALID_CONFIG", "is not a git revision"),
        ("main\nHEAD", "INVALID_CONFIG", "is not a git revision"),
        ("a" * 5000, "INVALID_CONFIG", "is not a git revision"),
    ],
)
def test_refs_that_name_no_commit_are_refused(
    repo: Path, base_ref: str, code: str, message: str
) -> None:
    with pytest.raises(SemanticLayerError, match=message) as refused:
        diff_package_report(
            PackageReference(source_path=str(repo / "semantic" / "shop")), base_ref=base_ref
        )
    assert refused.value.code == code


def test_a_package_outside_git_is_refused(tmp_path: Path) -> None:
    package = write_orders_package(tmp_path / "loose")

    with pytest.raises(SemanticLayerError, match="not inside a git repository"):
        diff_package_report(PackageReference(source_path=str(package)), base_ref="HEAD")


def test_the_extracted_package_is_removed_afterwards(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    impact_report(PackageReference(source_path=str(repo / "semantic" / "shop")), base_ref="HEAD")

    assert list(scratch.iterdir()) == []


def test_a_symlink_is_rejected_without_following_it(repo: Path, tmp_path: Path) -> None:
    package = repo / "semantic" / "shop"
    (package / "models" / "outside.yml").symlink_to(tmp_path / "elsewhere.yml")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "A symlink")
    (package / "models" / "outside.yml").unlink()
    destination = tmp_path / "extracted"
    destination.mkdir()

    with pytest.raises(SemanticLayerError, match="unsupported package tree entry") as refused:
        _extract_package_from_git(str(package), "HEAD", destination)
    assert refused.value.code == "INVALID_CONFIG"
    assert not (Path(destination) / "shop" / "models" / "outside.yml").exists()
    assert not (tmp_path / "elsewhere.yml").exists()


@pytest.mark.parametrize("entry_kind", ["symlink", "colon"])
@pytest.mark.parametrize("removed", [False, True])
def test_incomplete_baselines_fail_all_reports(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_kind: str, removed: bool
) -> None:
    package = repo / "semantic" / "shop"
    if entry_kind == "symlink":
        shared = package / "shared" / "extra.yml"
        shared.parent.mkdir()
        shared.write_text(yaml.safe_dump({"metrics": {"average_order": MARGIN}}))
        suspect = package / "metrics" / "extra.yml"
        suspect.symlink_to("../shared/extra.yml")
    else:
        suspect = package / "metrics" / "C:core.yml"
        suspect.write_text(yaml.safe_dump({"metrics": {"average_order": MARGIN}}))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Baseline with unsupported package entry")
    if removed:
        suspect.unlink()

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    ref = PackageReference(source_path=str(package))
    reports = (
        lambda: diff_package_report(ref, base_ref="HEAD"),
        lambda: impact_report(ref, base_ref="HEAD"),
        lambda: check_package_report(ref, base_ref="HEAD"),
        lambda: promote_package_report(ref, environment="staging", base_ref="HEAD"),
    )
    for report in reports:
        with pytest.raises(SemanticLayerError, match="cannot be compared") as refused:
            report()
        assert refused.value.code == "INVALID_CONFIG"
        assert list(scratch.iterdir()) == []


def test_materialization_error_fails_all_reports_and_cleans_up(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = repo / "semantic" / "shop"
    extra = package / "metrics" / "extra.yml"
    extra.write_text(yaml.safe_dump({"metrics": {"average_order": MARGIN}}))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Metric to delete")
    extra.unlink()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    original_write = Path.write_bytes

    def fail_metric_write(path: Path, data: bytes) -> int:
        if path.name == "extra.yml" and "semantic-rails-package-" in str(path):
            raise OSError("simulated storage failure")
        return original_write(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_metric_write)
    ref = PackageReference(source_path=str(package))
    for report in (
        lambda: diff_package_report(ref, base_ref="HEAD"),
        lambda: impact_report(ref, base_ref="HEAD"),
        lambda: check_package_report(ref, base_ref="HEAD"),
        lambda: promote_package_report(ref, environment="staging", base_ref="HEAD"),
    ):
        with pytest.raises(SemanticLayerError, match="could not materialize") as refused:
            report()
        assert refused.value.code == "INVALID_CONFIG"
        assert list(scratch.iterdir()) == []


def _crafted_commit(repo: Path, files: dict[bytes, bytes]) -> str:
    """A commit whose root tree names files exactly as given, as a crafted repository could."""
    tree = b""
    for name, content in sorted(files.items()):
        blob = _git(repo, "hash-object", "-w", "--stdin", stdin=content).strip()
        tree += b"100644 " + name + b"\0" + bytes.fromhex(blob)
    tree_id = _git(repo, "hash-object", "-t", "tree", "-w", "--literally", "--stdin", stdin=tree)
    return _git(repo, "commit-tree", tree_id.strip(), "-m", "crafted").strip()


def test_crafted_tree_names_fail_before_extraction(tmp_path: Path) -> None:
    repo = tmp_path / "crafted"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    outside = tmp_path / "outside.txt"
    commit = _crafted_commit(
        repo,
        {
            b"package.yml": b"schema_version: 1\n",
            str(outside).encode(): b"escaped",  # an absolute name
            b"../escape.txt": b"escaped",
            b"models//double.yml": b"escaped",
            b"C:evil.yml": b"escaped",
            b"caf\xe9.yml": b"a Latin-1 name",
        },
    )
    destination = tmp_path / "extracted"
    destination.mkdir()

    with pytest.raises(SemanticLayerError, match="unsupported package tree entry") as refused:
        _extract_package_from_git(str(repo), commit, destination)
    assert refused.value.code == "INVALID_CONFIG"
    assert not outside.exists()
    assert not (tmp_path / "escape.txt").exists() and not (destination / "escape.txt").exists()
    assert list(destination.iterdir()) == []


def test_normalization_equivalent_names_cannot_overwrite(tmp_path: Path) -> None:
    repo = tmp_path / "crafted"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    commit = _crafted_commit(
        repo,
        {
            b"package.yml": b"schema_version: 1\n",
            "metrics/é.yml".encode(): b"one",
            "metrics/e\u0301.yml".encode(): b"two",
        },
    )
    destination = tmp_path / "extracted"
    destination.mkdir()

    with pytest.raises(SemanticLayerError, match="ambiguous package tree name") as refused:
        _extract_package_from_git(str(repo), commit, destination)
    assert refused.value.code == "INVALID_CONFIG"
    assert list(destination.iterdir()) == []


def test_missing_blob_is_a_typed_comparison_error_with_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = write_orders_package(tmp_path / "crafted")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Package")
    blob = _git(repo, "hash-object", "-w", "--stdin", stdin=b"schema_version: 1\n").strip()
    tree = (
        b"100644 package.yml\0" + bytes.fromhex(blob) + b"100644 metrics/core.yml\0" + b"\x11" * 20
    )
    tree_id = _git(repo, "hash-object", "-t", "tree", "-w", "--literally", "--stdin", stdin=tree)
    commit = _git(repo, "commit-tree", tree_id.strip(), "-m", "missing blob").strip()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    with pytest.raises(SemanticLayerError, match="could not materialize") as refused:
        diff_package_report(PackageReference(source_path=str(repo)), base_ref=commit)
    assert refused.value.code == "INVALID_CONFIG"
    assert list(scratch.iterdir()) == []


def test_unchanged_ordinary_baseline_is_low_risk(repo: Path) -> None:
    ref = PackageReference(source_path=str(repo / "semantic" / "shop"))

    diff = diff_package_report(ref, base_ref="HEAD")
    impact = impact_report(ref, base_ref="HEAD")

    assert diff["changes"] == []
    assert impact["changes"] == []
    assert impact["impact"]["risk"] == "low"


def test_removed_metric_in_an_ordinary_baseline_is_high_risk(repo: Path) -> None:
    package = repo / "semantic" / "shop"
    extra = package / "metrics" / "extra.yml"
    extra.write_text(yaml.safe_dump({"metrics": {"average_order": MARGIN}}))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Additional metric")
    extra.unlink()

    ref = PackageReference(source_path=str(package))
    diff = diff_package_report(ref, base_ref="HEAD")
    impact = impact_report(ref, base_ref="HEAD")

    assert {(row["object_id"], row["change_type"]) for row in diff["changes"]} == {
        ("metric.shop.average_order", "removed")
    }
    assert impact["impact"]["risk"] == "high"


def test_a_package_directory_named_like_pathspec_magic(tmp_path: Path) -> None:
    root = tmp_path / "analytics"
    package = write_orders_package(root / ":(exclude)semantic")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "Package")
    _add_metric(package)

    report = diff_package_report(PackageReference(source_path=str(package)), base_ref="HEAD")

    assert [row["object_id"] for row in report["changes"]] == ["metric.shop.average_order"]


def test_repository_and_package_paths_keep_trailing_whitespace(tmp_path: Path) -> None:
    root = tmp_path / "analytics\n"
    package = write_orders_package(root / "semantic ")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "Package")
    _add_metric(package)

    report = diff_package_report(PackageReference(source_path=str(package)), base_ref="HEAD")

    assert [row["object_id"] for row in report["changes"]] == ["metric.shop.average_order"]
    assert report["comparison"]["source_path"].endswith(":semantic /shop")


def test_a_package_path_spelled_in_another_case(repo: Path) -> None:
    package = repo / "semantic" / "shop"
    other_case = Path(str(package).replace("/semantic/shop", "/SEMANTIC/shop"))
    if not other_case.exists():
        pytest.skip("case-sensitive file system")
    _add_metric(package)

    report = diff_package_report(PackageReference(source_path=str(other_case)), base_ref="HEAD")

    assert [row["object_id"] for row in report["changes"]] == ["metric.shop.average_order"]
