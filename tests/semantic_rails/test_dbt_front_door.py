"""A dbt project is the front door: the Architect and every loader read the DuckDB file
``dbt build`` writes at the dbt root, with no environment variable, and the hard wall
stays for anything outside the dbt project."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp_manager import detected_clients, mcp_client_config_report
from semantic_rails.runtime import Runtime
from tests.semantic_rails.dbt_warehouse import (
    DBT_PROJECT,
    build_dbt_warehouse,
    write_dbt_artifacts,
    write_orders_package,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV = "SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS"


@pytest.fixture(autouse=True)
def _no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)


def _dbt_project(root: Path, *, profile_path: str | None = "warehouse.duckdb") -> Path:
    """A dbt-duckdb project as ``dbt build`` leaves it: the file at the dbt root."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "dbt_project.yml").write_text("name: shop_dbt\nprofile: shop_dbt\n", encoding="utf-8")
    if profile_path is not None:
        profile = {"shop_dbt": {"target": "dev", "outputs": {"dev": {"type": "duckdb"}}}}
        profile["shop_dbt"]["outputs"]["dev"]["path"] = profile_path
        (root / "profiles.yml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    build_dbt_warehouse(root / "warehouse.duckdb")
    return root


def _calls(server: Any, calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:
            return [
                dict((await session.call_tool(name, arguments)).structuredContent or {})
                for name, arguments in calls
            ]

    return asyncio.run(run())


def _order_count(package: Path) -> int:
    runtime = Runtime.from_path(str(package))
    try:
        rows = runtime.query(
            {"select": [{"expression": "measure.shop.order_count", "as": "orders"}]}
        )["rows"]
    finally:
        runtime.close()
    return int(rows[0]["orders"])


def test_a_package_inside_a_dbt_project_reads_the_file_dbt_builds(tmp_path: Path) -> None:
    dbt = _dbt_project(tmp_path / "jaffle")
    package = write_orders_package(dbt / "semantic", seed={"kind": "external"})
    _set_default_db(package, "../../warehouse.duckdb")

    assert load_package_config(str(package)).package.default_db == "../../warehouse.duckdb"
    assert _order_count(package) == 8


def _set_default_db(package: Path, default_db: str) -> None:
    path = package / "package.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["package"]["default_db"] = default_db
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("layout", "seed", "default_db"),
    [
        ("no_dbt_project", {"kind": "external"}, "../../warehouse.duckdb"),
        ("dbt_project", {"kind": "external"}, "../../../warehouse.duckdb"),
        ("dbt_project", None, "../../warehouse.duckdb"),
        # A link inside the dbt project that points outside it: the real path decides.
        ("symlinked_file", {"kind": "external"}, "../../link.duckdb"),
        ("symlinked_dir", {"kind": "external"}, "../../linkdir/secret.duckdb"),
    ],
    ids=[
        "outside_any_dbt_project",
        "escapes_the_dbt_project",
        "seeded_database",
        "symlinked_file_points_outside",
        "symlinked_directory_points_outside",
    ],
)
def test_the_wall_stays_outside_the_dbt_project(
    tmp_path: Path, layout: str, seed: dict[str, Any] | None, default_db: str
) -> None:
    root = tmp_path / "jaffle"
    if layout == "no_dbt_project":
        build_dbt_warehouse(root / "warehouse.duckdb")
    else:
        _dbt_project(root)
    if layout.startswith("symlinked"):
        outside = tmp_path / "outside"
        build_dbt_warehouse(outside / "secret.duckdb")
        (root / "link.duckdb").symlink_to(outside / "secret.duckdb")
        (root / "linkdir").symlink_to(outside, target_is_directory=True)
    package = write_orders_package(root / "semantic", seed=seed)
    _set_default_db(package, default_db)

    with pytest.raises(SemanticLayerError) as excinfo:
        load_package_config(str(package))
    assert excinfo.value.code == "INVALID_CONFIG"
    message = str(excinfo.value)
    assert f"package.default_db must not contain '..' path segments (got {default_db!r})" in message
    assert "only to a file inside the dbt project that holds the package" in message
    assert f"Set {ENV}=1" in message


def _key_staging_model(target_dir: Path, name: str, column: str) -> None:
    """Give a staging model a key in dbt (unique and not_null tests), as jaffle_shop's
    staging models have, so it drafts the same model id as its mart."""
    path = target_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    model = f"model.{DBT_PROJECT}.{name}"
    for kind in ("unique", "not_null"):
        unique_id = f"test.{DBT_PROJECT}.{kind}_{name}_{column}"
        manifest["nodes"][unique_id] = {
            "unique_id": unique_id,
            "resource_type": "test",
            "name": f"{kind}_{name}_{column}",
            "column_name": column,
            "attached_node": model,
            "test_metadata": {"name": kind, "namespace": None, "kwargs": {"column_name": column}},
            "depends_on": {"nodes": [model]},
        }
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _dialog_select(action: str) -> list[str]:
    found = re.search(r"select (\[.*?\]) to bring in", action)
    assert found, action
    return list(json.loads(found.group(1)))


def test_the_dialog_reads_dbt_and_its_draft_creates_a_package_that_answers(
    tmp_path: Path,
) -> None:
    dbt = _dbt_project(tmp_path)
    write_dbt_artifacts(dbt / "warehouse.duckdb", dbt / "target")
    # stg_orders has a key too and drafts the model id "orders", like fct_orders.
    _key_staging_model(dbt / "target", "stg_orders", "order_id")
    server = create_architect_mcp_server(workspace_root=tmp_path)

    (dialog,) = _calls(server, [("setup_project_dialog", {"package_id": "shop"})])

    assert dialog["ok"] is True, dialog
    assert dialog["dbt_project"]["name"] == "shop_dbt"
    assert dialog["dbt_project"]["manifest"] is True
    asked = [row["id"] for row in dialog["questions"]]
    assert asked == ["package_id", "description"]
    draft = dialog["draft_arguments"]
    assert draft["project_path"] == "semantic_rails/shop"
    assert draft["data"] == "external"
    assert draft["default_db"] == "../../warehouse.duckdb"
    assert draft["relation"] == "main_marts.fct_orders"
    # dbt's column types name the time column, so the first package answers by month.
    assert draft["time_column"] == "ordered_at"
    assert dialog["answered_by_dbt"]["time_column"] == "ordered_at"
    action = dialog["next_actions"][0]
    assert "import_dbt_project" in action
    select = _dialog_select(action)
    # One dbt model per drafted model id: the mart wins over its staging model.
    assert f"model.{DBT_PROJECT}.fct_orders" in select
    assert f"model.{DBT_PROJECT}.stg_orders" not in select
    assert (
        "Left out because another selected dbt model drafts the same model id: "
        f"model.{DBT_PROJECT}.stg_orders (as orders)."
    ) in action

    created, status = _calls(
        server,
        [
            ("create_project", {**draft, "idempotency_key": "create", "dry_run": False}),
            ("project_status", {"project_path": draft["project_path"]}),
        ],
    )
    assert created["ok"] is True, created
    # The dialog's own next step, exactly as it words it.
    imported, validated = _calls(
        server,
        [
            (
                "import_dbt_project",
                {
                    "project_path": draft["project_path"],
                    "select": select,
                    "target_dir": "target",
                    "expected_revision": status["revision"],
                    "idempotency_key": "import",
                },
            ),
            ("validate_project", {"project_path": draft["project_path"], "mode": "runtime"}),
        ],
    )
    assert imported["ok"] is True, imported
    assert validated["ok"] is True, validated
    runtime = Runtime.from_path(str(tmp_path / draft["project_path"]))
    try:
        by_month = runtime.query(
            {
                "select": [{"expression": "measure.shop.order_count", "as": "orders"}],
                "time": {"grain": "month"},
            }
        )
    finally:
        runtime.close()
    assert sum(int(row["orders"]) for row in by_month["rows"]) == 8, by_month
    # The CLI and the query MCP load the same package in a fresh shell, with no variable set.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semantic_rails",
            "project",
            "validate",
            "--path",
            "semantic_rails/shop",
            "--json",
        ],
        cwd=str(tmp_path),
        env={
            **{k: v for k, v in os.environ.items() if k != ENV},
            "PYTHONPATH": str(REPO_ROOT),
            "SEMANTIC_RAILS_HOME": str(tmp_path / "home"),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_the_dialog_finds_the_file_by_database_name_and_asks_for_a_manifest(
    tmp_path: Path,
) -> None:
    dbt = _dbt_project(tmp_path / "jaffle", profile_path=None)
    server = create_architect_mcp_server(workspace_root=tmp_path)

    (before,) = _calls(server, [("setup_project_dialog", {"package_id": "shop"})])
    assert before["dbt_project"]["manifest"] is False
    assert before["next_actions"][0].startswith("Run `dbt parse` (or `dbt build`) in jaffle/")
    # Without a manifest dbt can't answer the first model, so those questions stay.
    assert "relation" in [row["id"] for row in before["questions"]]

    write_dbt_artifacts(dbt / "warehouse.duckdb", dbt / "target")
    (after,) = _calls(server, [("setup_project_dialog", {"package_id": "shop"})])
    assert after["draft_arguments"]["project_path"] == "jaffle/semantic_rails/shop"
    # manifest database "warehouse" names the file dbt-duckdb builds.
    assert after["draft_arguments"]["default_db"] == "../../warehouse.duckdb"
    assert "relation" not in [row["id"] for row in after["questions"]]


@pytest.mark.parametrize(
    ("profile_path", "time_column"),
    [("warehouse.duckdb", "ordered_at"), ("not_built_yet.duckdb", "")],
    ids=["types_from_the_duckdb_file", "no_types_anywhere"],
)
def test_without_dbt_column_types_the_dialog_reads_the_file_or_asks_for_time(
    tmp_path: Path, profile_path: str, time_column: str
) -> None:
    dbt = _dbt_project(tmp_path, profile_path=profile_path)
    write_dbt_artifacts(dbt / "warehouse.duckdb", dbt / "target")
    (dbt / "target" / "catalog.json").unlink()  # `dbt build` alone writes no catalog

    (dialog,) = _calls(
        create_architect_mcp_server(workspace_root=tmp_path),
        [("setup_project_dialog", {"package_id": "shop"})],
    )

    assert dialog["draft_arguments"]["time_column"] == time_column
    asked = {row["id"]: row for row in dialog["questions"]}
    if time_column:
        assert "time_column" not in asked
        assert dialog["answered_by_dbt"]["time_column"] == time_column
    else:
        # Nothing names a time column: the question stays, drafted blank (not a guess).
        assert asked["time_column"]["default"] == ""
        assert "time_column" not in dialog["answered_by_dbt"]
    assert "relation" not in asked


def test_no_dbt_project_keeps_the_full_dialog(tmp_path: Path) -> None:
    (dialog,) = _calls(
        create_architect_mcp_server(workspace_root=tmp_path),
        [("setup_project_dialog", {"package_id": "shop"})],
    )
    assert "dbt_project" not in dialog
    assert "relation" in [row["id"] for row in dialog["questions"]]


def test_create_project_writes_an_absolute_database_path_relative(tmp_path: Path) -> None:
    dbt = _dbt_project(tmp_path)
    (created,) = _calls(
        create_architect_mcp_server(workspace_root=tmp_path),
        [
            (
                "create_project",
                {
                    "package_id": "shop",
                    "project_path": "semantic/shop",
                    "expected_revision": "absent",
                    "idempotency_key": "abs",
                    "data": "external",
                    "default_db": str(dbt / "warehouse.duckdb"),
                    "first_entity": "order",
                    "relation": "main_marts.fct_orders",
                    "primary_key": "order_id",
                    "time_column": "",
                },
            )
        ],
    )
    assert created["ok"] is True, created
    package = yaml.safe_load((tmp_path / "semantic/shop/package.yml").read_text())
    assert package["package"]["default_db"] == "../../warehouse.duckdb"


def test_the_architect_defaults_to_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (config,) = _calls(create_architect_mcp_server(), [("mcp_client_config", {})])
    assert config["workspace_root"] == str(tmp_path.resolve())


def _client_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, which: set[str]) -> None:
    import semantic_rails.mcp_manager as manager

    for name in ("CLAUDE", "CODEX", "CURSOR"):
        monkeypatch.setenv(f"SEMANTIC_RAILS_{name}_CONFIG", str(tmp_path / name / "config"))
    monkeypatch.setattr(
        manager.shutil, "which", lambda name: f"/bin/{name}" if name in which else None
    )


def test_auto_client_is_every_client_installed_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client_dirs(tmp_path, monkeypatch, which={"claude"})
    (tmp_path / "CODEX").mkdir()
    assert detected_clients() == ["codex", "claude-code"]

    report = mcp_client_config_report(
        None, client="auto", mcp="architect", workspace_root=str(tmp_path)
    )
    assert sorted(report["previews"]) == ["claude-code", "codex"]
    assert list(report["servers"]) == ["semantic-rails-architect"]
    assert report["package"] is None


def test_auto_client_with_none_installed_names_the_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client_dirs(tmp_path, monkeypatch, which=set())
    with pytest.raises(SemanticLayerError) as excinfo:
        detected_clients()
    assert excinfo.value.code == "INVALID_CONFIG"
    assert str(excinfo.value).startswith("No MCP client found on this machine")
    assert "Pass --client claude-code, claude, codex or cursor." in str(excinfo.value)


def test_the_query_mcp_still_needs_a_package() -> None:
    with pytest.raises(SemanticLayerError) as excinfo:
        mcp_client_config_report(None, client="codex", mcp="both")
    assert excinfo.value.code == "INVALID_CONFIG"
    assert str(excinfo.value).startswith(
        "The query MCP answers from one package: pass --package or --path"
    )


def test_mcp_setup_for_the_architect_needs_no_package(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "SEMANTIC_RAILS_HOME": str(tmp_path / "home"),
        "SEMANTIC_RAILS_CODEX_CONFIG": str(tmp_path / "codex" / "config.toml"),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semantic_rails",
            "mcp",
            "setup",
            "--mcp",
            "architect",
            "--client",
            "codex",
            "--json",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["package"] is None
    assert payload["client_config"]["workspace_root"] == str(tmp_path.resolve())
    server = payload["client_config"]["servers"]["semantic-rails-architect"]
    assert server["args"][-2:] == ["--workspace-root", str(tmp_path.resolve())]


def test_mcp_setup_says_which_existing_servers_it_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import semantic_rails.mcp_manager as manager
    from semantic_rails.cli.commands.mcp import _print_mcp_setup_report

    _client_dirs(tmp_path, monkeypatch, which={"claude"})
    desktop = tmp_path / "CLAUDE" / "config"
    desktop.parent.mkdir()
    other_workspace = {"command": "python", "args": ["--workspace-root", "/work/first_dbt"]}
    desktop.write_text(
        json.dumps({"mcpServers": {"semantic-rails-architect": other_workspace, "x": {}}})
    )
    codex = tmp_path / "CODEX" / "config"
    codex.parent.mkdir()
    codex.write_text('[mcp_servers.other]\ncommand = "x"\n')
    registered = {"semantic-rails-architect"}

    def claude(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        verb, name = args[2], args[5]
        if verb == "add-json" and name in registered:
            return subprocess.CompletedProcess(args, 1, "", f"MCP server {name} already exists")
        (registered.add if verb == "add-json" else registered.discard)(name)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager.subprocess, "run", claude)

    preview = mcp_client_config_report(
        None, client="auto", mcp="architect", workspace_root=str(tmp_path)
    )
    assert preview["previews"]["claude"]["replaces"] == ["semantic-rails-architect"]
    assert preview["previews"]["codex"]["replaces"] == []  # only another server is there
    assert preview["previews"]["claude-code"]["replaces_any_named"] == ["semantic-rails-architect"]
    _print_mcp_setup_report({"ok": True, "mode": "preview", "client_config": preview})
    text = capsys.readouterr().out
    assert "    Replaces the server already there: semantic-rails-architect" in text
    assert "    Replaces any user-scope server already named: semantic-rails-architect" in text

    report = mcp_client_config_report(
        None, client="auto", mcp="architect", workspace_root=str(tmp_path), install=True
    )
    installed = report["installed"]
    assert installed["claude"]["replaced"] == ["semantic-rails-architect"]
    assert installed["codex"]["replaced"] == []
    assert installed["claude-code"]["replaced"] == ["semantic-rails-architect"]
    _print_mcp_setup_report({"ok": True, "mode": "install", "client_config": report})
    text = capsys.readouterr().out
    assert text.count("    Replaced: semantic-rails-architect") == 2
    # Run again: now Codex has it too, and says so.
    again = mcp_client_config_report(
        None, client="codex", mcp="architect", workspace_root=str(tmp_path)
    )
    assert again["previews"]["codex"]["replaces"] == ["semantic-rails-architect"]
