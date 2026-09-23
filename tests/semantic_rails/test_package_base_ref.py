"""base_ref comparisons resolve in the package's own git repository."""

from __future__ import annotations

import asyncio
import os
import subprocess
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
    diff_package_report,
    impact_report,
)
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse, write_orders_package

MARGIN = {
    "label": "Average order",
    "description": "Order total per order.",
    "kind": "aggregate",
    "measure": "order_total",
    "value_type": "currency",
}


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    ).stdout


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
            for name in ("diff_project", "impact_project"):
                result = await session.call_tool(
                    name, {"project_path": "semantic/shop", "base_ref": "HEAD~1"}
                )
                results.append(dict(result.structuredContent or {}))
            return results

    diff, impact = asyncio.run(run())

    assert diff["ok"] is True, diff
    assert [row["object_id"] for row in diff["changes"]] == ["metric.shop.average_order"]
    assert impact["ok"] is True, impact
    assert impact["impact"]["risk"] == "high"


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


def test_only_regular_files_are_extracted(repo: Path, tmp_path: Path) -> None:
    package = repo / "semantic" / "shop"
    (package / "models" / "outside.yml").symlink_to(tmp_path / "elsewhere.yml")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "A symlink")
    (package / "models" / "outside.yml").unlink()
    destination = tmp_path / "extracted"
    destination.mkdir()

    extracted, _ = _extract_package_from_git(str(package), "HEAD", destination)

    assert (Path(extracted) / "models" / "orders.yml").is_file()
    assert not (Path(extracted) / "models" / "outside.yml").exists()
    assert not (Path(extracted) / "data" / "warehouse.duckdb").exists()  # ignored in git
