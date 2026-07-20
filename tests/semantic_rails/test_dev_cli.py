"""Developer-facing CLI workflows.

The lower-level CLI mirrors the JSON runtime surfaces. These tests pin
the human developer layer that wraps package creation, discovery, setup,
and validation.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "semantic_rails", *args],
        cwd=str(REPO_ROOT),
        env=run_env,
        capture_output=True,
        text=True,
    )


def _run_json(*args: str, env: dict[str, str] | None = None) -> dict:
    proc = _run_cli(*args, env=env)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip(), f"CLI produced no stdout for args={args!r}"
    return json.loads(proc.stdout)


def _run_cli_in(
    cwd: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    run_env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "semantic_rails", *args],
        cwd=str(cwd),
        env=run_env,
        capture_output=True,
        text=True,
    )


def _run_json_in(cwd: Path, *args: str, env: dict[str, str] | None = None) -> dict:
    proc = _run_cli_in(cwd, *args, env=env)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip(), f"CLI produced no stdout for args={args!r}"
    return json.loads(proc.stdout)


def test_project_new_creates_split_package_and_validates(tmp_path: Path) -> None:
    payload = _run_json(
        "project",
        "new",
        "growth_core",
        "--workspace-root",
        str(tmp_path),
        "--json",
    )

    project_path = tmp_path / "growth_core"
    assert payload["ok"] is True, payload
    assert payload["project_path"] == str(project_path)
    assert (project_path / "package.yml").is_file()
    assert (project_path / "graph.yml").is_file()
    assert (project_path / ".gitignore").read_text(encoding="utf-8").splitlines() == [
        "data/*.duckdb",
        "data/*.sqlite",
        "data/*.sqlite3",
        ".compiled/",
    ]
    assert (project_path / "models" / "core" / "events.yml").is_file()
    assert payload["checks"]["parse"]["ok"] is True
    assert payload["checks"]["examples"]["passed"] == 1
    assert payload["checks"]["tests"]["passed"] == 2

    validate = _run_json(
        "project",
        "validate",
        "--path",
        str(project_path),
        "--mode",
        "full",
        "--json",
    )
    assert validate["ok"] is True, validate
    assert validate["checks"]["validate"]["ok"] is True


def test_init_name_creates_split_package_by_default(tmp_path: Path) -> None:
    payload = _run_json(
        "init",
        "init_core",
        "--workspace-root",
        str(tmp_path),
        "--yes",
        "--json",
    )

    project_path = tmp_path / "init_core"
    assert payload["ok"] is True, payload
    assert payload["project_path"] == str(project_path)
    assert (project_path / "graph.yml").is_file()
    assert (project_path / "models" / "core" / "events.yml").is_file()


def test_legacy_init_output_still_creates_single_file_package(tmp_path: Path) -> None:
    target = tmp_path / "single_file_core"
    payload = _run_json(
        "init",
        "--output",
        str(target),
        "--package-id",
        "single_file_core",
    )

    assert payload["ok"] is True, payload
    assert (target / "package.yml").is_file()
    assert not (target / "graph.yml").exists()


def test_single_file_flag_with_name_uses_legacy_scaffold(tmp_path: Path) -> None:
    target = tmp_path / "named_single"
    payload = _run_json(
        "init",
        "named_single",
        "--single-file",
        "--output",
        str(target),
        "--json",
    )

    assert payload["ok"] is True, payload
    assert payload["package_id"] == "named_single"
    assert (target / "package.yml").is_file()
    assert not (target / "graph.yml").exists()


def test_project_list_discovers_packages_under_extra_root(tmp_path: Path) -> None:
    _run_json(
        "project",
        "new",
        "discover_core",
        "--workspace-root",
        str(tmp_path),
        "--skip-checks",
        "--json",
    )

    payload = _run_json("project", "list", "--root", str(tmp_path), "--with-status", "--json")
    discovered = [row for row in payload["packages"] if row["id"] == "discover_core"]

    assert discovered, payload
    assert discovered[0]["origin"] == "discovered"
    assert discovered[0]["ok"] is True
    assert discovered[0]["summary"]["entities"] == 1


def test_commands_default_to_current_package_directory(tmp_path: Path) -> None:
    _run_json(
        "init",
        "local_pkg",
        "--workspace-root",
        str(tmp_path),
        "--yes",
        "--skip-checks",
        "--json",
    )
    project_path = tmp_path / "local_pkg"

    listed = _run_cli_in(project_path, "ls", "--json")
    assert listed.returncode == 0, listed.stderr
    payload = json.loads(listed.stdout)

    assert payload["package"]["id"] == "local_pkg"
    assert payload["package"]["source_path"] == str(project_path)


def test_local_profile_can_be_initialized_and_used_as_cli_default(tmp_path: Path) -> None:
    env = {"SEMANTIC_RAILS_HOME": str(tmp_path / "semantic_rails_home")}
    _run_json(
        "init",
        "profile_core",
        "--workspace-root",
        str(tmp_path),
        "--yes",
        "--skip-checks",
        "--json",
        env=env,
    )
    project_path = tmp_path / "profile_core"

    profile = _run_json(
        "profile",
        "init",
        "--package-path",
        str(project_path),
        "--json",
        env=env,
    )
    profile_path = Path(profile["path"])

    assert profile["exists"] is True
    assert profile["resolved_package_path"] == str(project_path)
    assert profile_path == tmp_path / "semantic_rails_home" / "profiles.yml"
    assert "Use package.yml plus warehouse CLI/env credentials" in profile_path.read_text(
        encoding="utf-8"
    )

    planned = _run_json(
        "ask",
        "total amount by event type",
        "--json",
        env=env,
    )
    assert planned["ok"] is True, planned
    assert planned["package"]["source_path"] == str(project_path)


def test_local_profile_rejects_non_package_connection_modes(tmp_path: Path) -> None:
    from semantic_rails.errors import SemanticLayerError
    from semantic_rails.local_config import load_local_profiles, resolve_local_package_path

    profiles_path = tmp_path / "profiles.yml"
    profiles_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "defaults": {"profile": "remote", "target": "dev"},
                "profiles": {
                    "remote": {
                        "targets": {
                            "dev": {
                                "package_path": str(tmp_path / "package"),
                                "connection": {"mode": "service"},
                            }
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(SemanticLayerError, match="package targets only"):
        resolve_local_package_path(load_local_profiles(profiles_path))


def test_setup_without_registered_package_points_to_init(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import semantic_rails.dev_cli as dev_cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SEMANTIC_RAILS_HOME", str(tmp_path / "empty_home"))
    monkeypatch.setattr(dev_cli, "list_package_paths", lambda: {})

    report = dev_cli.setup_report(
        argparse.Namespace(package="", path="", checks="parse", server=False)
    )

    assert report["ok"] is True, report
    assert [check for check in report["checks"] if check["name"] == "registered_packages"][0][
        "packages"
    ] == []
    assert [check for check in report["checks"] if check["name"] == "package_parse"][0][
        "summary"
    ] == "no package selected"
    assert "semantic-rails init my_package --yes" in report["next_actions"]


def test_mcp_doctor_loads_path_package_and_lists_tools(tmp_path: Path) -> None:
    _run_json(
        "init",
        "mcp_core",
        "--workspace-root",
        str(tmp_path),
        "--yes",
        "--skip-checks",
        "--json",
    )
    project_path = tmp_path / "mcp_core"

    payload = _run_json("mcp", "doctor", "--path", str(project_path))

    assert payload["ok"] is True, payload
    assert payload["package"]["source_path"] == str(project_path)
    assert payload["mcp"]["tool_count"] >= 13
    assert payload["mcp"]["required_tools_present"] is True
    assert "supported" in payload["managed_lifecycle"]
    if payload["managed_lifecycle"]["supported"]:
        assert any(" mcp start " in command for command in payload["next_commands"])
    else:
        assert not any(" mcp start " in command for command in payload["next_commands"])


def test_mcp_doctor_uses_windows_safe_foreground_commands(
    runtime_factory, monkeypatch, capsys
) -> None:
    import semantic_rails.cli as cli

    runtime = runtime_factory("jaffle_shop")
    monkeypatch.setattr(cli, "_runtime_from_package_or_path", lambda _args: runtime)
    monkeypatch.setattr(
        cli,
        "managed_mcp_lifecycle_report",
        lambda: {
            "supported": False,
            "platform": "win32",
            "mode": "foreground-only",
            "reason": "Managed lifecycle is unavailable.",
            "windows_alternative": "Use stdio or foreground HTTP.",
        },
    )

    cli.cmd_mcp_doctor(argparse.Namespace(package="jaffle_shop", path=""))
    payload = json.loads(capsys.readouterr().out)

    assert payload["managed_lifecycle"]["supported"] is False
    assert not any(" mcp start " in command for command in payload["next_commands"])
    assert not any(" mcp stop" in command for command in payload["next_commands"])
    assert any("mcp stdio" in command for command in payload["next_commands"])
    assert any("--install --yes" in command for command in payload["next_commands"])


def test_interactive_setup_does_not_offer_managed_start_when_unsupported(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import semantic_rails.dev_cli as dev_cli
    from semantic_rails.config_validation import PackageReference

    prompts: list[str] = []
    monkeypatch.setattr(dev_cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(
        dev_cli,
        "_interactive_package_ref",
        lambda _args: PackageReference(source_path=str(tmp_path / "package")),
    )
    monkeypatch.setattr(
        dev_cli,
        "managed_mcp_lifecycle_report",
        lambda: {
            "supported": False,
            "platform": "win32",
            "mode": "foreground-only",
            "reason": "Managed lifecycle is unavailable.",
            "windows_alternative": "Use stdio or foreground HTTP.",
        },
    )

    def decline(label: str, *, default: bool) -> bool:
        prompts.append(label)
        return False

    monkeypatch.setattr(dev_cli, "_confirm", decline)
    monkeypatch.setattr(dev_cli, "_prompt_choice", lambda *a, **k: "none")
    monkeypatch.setattr(
        dev_cli,
        "start_mcp_http_server",
        lambda *a, **k: pytest.fail("managed server must not start on an unsupported platform"),
    )

    dev_cli.cmd_setup_interactive(SimpleNamespace(json=False))
    output = capsys.readouterr().out

    assert not any("Start a managed" in prompt for prompt in prompts)
    assert "POSIX-only" in output
    assert "Windows" in output
    assert "stdio" in output
    assert f"semantic-rails repl --path {tmp_path / 'package'}" in output


def test_interactive_setup_cleans_dead_mcp_registration_and_retries(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import semantic_rails.dev_cli as dev_cli
    from semantic_rails.config_validation import PackageReference
    from semantic_rails.errors import SemanticLayerError

    ref = PackageReference(source_path=str(tmp_path / "package"))
    starts: list[str] = []
    stops: list[str] = []

    def start(*_args, **_kwargs):
        starts.append("start")
        if len(starts) == 1:
            raise SemanticLayerError(
                "CONFIG_CONFLICT",
                "dead registration",
                details={"server": {"pid_alive": False}},
            )
        return {"ok": True, "status": "started"}

    monkeypatch.setattr(dev_cli, "start_mcp_http_server", start)
    monkeypatch.setattr(
        dev_cli,
        "stop_mcp_http_server",
        lambda *, name: stops.append(name) or {"ok": True, "status": "not_running"},
    )
    monkeypatch.setattr(dev_cli, "_confirm", lambda *_args, **_kwargs: True)

    dev_cli._start_managed_mcp_from_wizard(ref)

    output = capsys.readouterr().out
    assert "dead 'default' MCP registration" in output
    assert "Stale registration: not_running" in output
    assert '"status": "started"' in output
    assert starts == ["start", "start"]
    assert stops == ["default"]


def test_interactive_setup_keeps_live_mcp_conflict_nonfatal(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import semantic_rails.dev_cli as dev_cli
    from semantic_rails.config_validation import PackageReference
    from semantic_rails.errors import SemanticLayerError

    monkeypatch.setattr(
        dev_cli,
        "start_mcp_http_server",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SemanticLayerError(
                "CONFIG_CONFLICT",
                "live server has another configuration",
                details={"server": {"pid_alive": True}},
            )
        ),
    )
    monkeypatch.setattr(
        dev_cli,
        "stop_mcp_http_server",
        lambda **_kwargs: pytest.fail("a live conflicting registration must not be removed"),
    )

    dev_cli._start_managed_mcp_from_wizard(PackageReference(source_path=str(tmp_path / "package")))

    output = capsys.readouterr().out
    assert "MCP server not started" in output
    assert "Setup will continue" in output
    assert "semantic-rails mcp status" in output


def test_mcp_status_and_client_config_install_use_local_files(tmp_path: Path) -> None:
    env = {
        "SEMANTIC_RAILS_HOME": str(tmp_path / "semantic_rails_home"),
        "SEMANTIC_RAILS_CLAUDE_CONFIG": str(tmp_path / "Claude" / "claude_desktop_config.json"),
        "SEMANTIC_RAILS_CODEX_CONFIG": str(tmp_path / "codex" / "config.toml"),
    }
    _run_json(
        "init",
        "client_core",
        "--workspace-root",
        str(tmp_path),
        "--yes",
        "--skip-checks",
        "--json",
        env=env,
    )
    project_path = tmp_path / "client_core"

    status = _run_json("mcp", "status", "--path", str(project_path), env=env)
    available = {row["name"] for row in status["available"]}
    assert "semantic-rails-query-stdio" in available
    assert "semantic-rails-query-http" in available
    assert "semantic-rails-architect-stdio" in available

    installed = _run_json(
        "mcp",
        "client-config",
        "--path",
        str(project_path),
        "--client",
        "both",
        "--mcp",
        "both",
        "--install",
        "--yes",
        env=env,
    )

    claude = json.loads(Path(env["SEMANTIC_RAILS_CLAUDE_CONFIG"]).read_text(encoding="utf-8"))
    codex = Path(env["SEMANTIC_RAILS_CODEX_CONFIG"]).read_text(encoding="utf-8")
    assert installed["installed"]["claude"]["ok"] is True
    assert installed["installed"]["codex"]["ok"] is True
    assert "semantic-rails" in claude["mcpServers"]
    assert "semantic-rails-architect" in claude["mcpServers"]
    assert str(project_path) in json.dumps(claude)
    assert "[mcp_servers.semantic_rails]" in codex
    assert "[mcp_servers.semantic_rails_architect]" in codex
    assert str(project_path) in codex


def test_mcp_setup_installs_client_configs_with_one_command(tmp_path: Path) -> None:
    env = {
        "SEMANTIC_RAILS_HOME": str(tmp_path / "semantic_rails_home"),
        "SEMANTIC_RAILS_CLAUDE_CONFIG": str(tmp_path / "Claude" / "claude_desktop_config.json"),
        "SEMANTIC_RAILS_CODEX_CONFIG": str(tmp_path / "codex" / "config.toml"),
    }
    _run_json(
        "init",
        "setup_core",
        "--workspace-root",
        str(tmp_path),
        "--yes",
        "--skip-checks",
        "--json",
        env=env,
    )
    project_path = tmp_path / "setup_core"

    payload = _run_json(
        "mcp",
        "setup",
        "--path",
        str(project_path),
        "--client",
        "both",
        "--mcp",
        "both",
        "--install",
        "--yes",
        "--json",
        env=env,
    )

    assert payload["ok"] is True, payload
    assert payload["mode"] == "install"
    assert payload["mcp"]["required_tools_present"] is True
    assert payload["client_config"]["installed"]["claude"]["ok"] is True
    assert payload["client_config"]["installed"]["codex"]["ok"] is True
    assert "Restart the selected MCP client" in payload["next_commands"][0]


def test_mcp_setup_uses_current_package_without_path(tmp_path: Path) -> None:
    env = {
        "SEMANTIC_RAILS_HOME": str(tmp_path / "semantic_rails_home"),
        "SEMANTIC_RAILS_CLAUDE_CONFIG": str(tmp_path / "Claude" / "claude_desktop_config.json"),
        "SEMANTIC_RAILS_CODEX_CONFIG": str(tmp_path / "codex" / "config.toml"),
    }
    _run_json(
        "init",
        "cwd_core",
        "--workspace-root",
        str(tmp_path),
        "--yes",
        "--skip-checks",
        "--json",
        env=env,
    )
    project_path = tmp_path / "cwd_core"

    payload = _run_json_in(
        project_path,
        "mcp",
        "setup",
        "--client",
        "codex",
        "--mcp",
        "query",
        "--json",
        env=env,
    )

    assert payload["ok"] is True, payload
    assert payload["mode"] == "preview"
    assert payload["package"]["source_path"] == str(project_path)
    assert sorted(payload["client_config"]["previews"]) == ["codex"]
    assert "semantic-rails mcp setup --path" in payload["next_commands"][0]


def test_dbt_style_debug_ls_and_ask_commands_are_human_readable() -> None:
    debug = _run_cli("debug", "--checks", "none")
    assert debug.returncode == 0, debug.stderr
    assert "Semantic Rails debug" in debug.stdout

    listed = _run_cli(
        "ls",
        "--package",
        "jaffle_shop",
        "--resource-type",
        "metric",
        "--select",
        "revenue",
        "--limit",
        "3",
    )
    assert listed.returncode == 0, listed.stderr
    assert "metric.sales.cumulative_revenue" in listed.stdout
    assert "use --limit 0 or --json" in listed.stdout

    planned = _run_json("ask", "--package", "jaffle_shop", "monthly revenue by store", "--json")
    assert planned["ok"] is True, planned
    assert planned["plan"]["pattern"]
    assert planned["query"]["group_by"] == ["dimension.jaffle_store_name"]


def test_ls_time_alias_and_ask_compile_have_real_output() -> None:
    times = _run_cli("ls", "--package", "jaffle_shop", "--resource-type", "time", "--limit", "2")
    assert times.returncode == 0, times.stderr
    assert "temporal_role." in times.stdout
    assert "0 time object" not in times.stdout

    compiled = _run_json(
        "ask",
        "--package",
        "jaffle_shop",
        "monthly revenue by store",
        "--compile",
        "--json",
    )
    assert compiled["ok"] is True, compiled
    assert "SELECT" in compiled["compile"]["sql"]


def test_adversarial_argument_validation_and_json_errors(tmp_path: Path) -> None:
    negative_limit = _run_cli(
        "ls",
        "--package",
        "jaffle_shop",
        "--limit",
        "-1",
        "--json",
    )
    assert negative_limit.returncode == 1
    assert json.loads(negative_limit.stdout)["error"]["code"] == "INVALID_CONFIG"

    bad_root = tmp_path / "not_a_dir"
    bad_root.write_text("not a directory\n", encoding="utf-8")
    invalid_workspace = _run_cli(
        "project",
        "new",
        "bad_root",
        "--workspace-root",
        str(bad_root),
        "--skip-checks",
        "--json",
    )
    assert invalid_workspace.returncode == 1
    assert json.loads(invalid_workspace.stdout)["error"]["code"] == "INVALID_CONFIG"

    conflicting_ask = _run_cli(
        "ask",
        "--package",
        "jaffle_shop",
        "monthly revenue by store",
        "--run",
        "--compile",
        "--json",
    )
    assert conflicting_ask.returncode == 1
    assert json.loads(conflicting_ask.stdout)["error"]["code"] == "INVALID_CONFIG"


def test_project_new_rejects_symlink_overwrite_with_force(tmp_path: Path) -> None:
    target = tmp_path / "evil_pkg"
    target.mkdir()
    victim = tmp_path / "victim.yml"
    victim.write_text("keep me\n", encoding="utf-8")
    try:
        os.symlink(victim, target / "package.yml")
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    proc = _run_cli(
        "project",
        "new",
        "evil_pkg",
        "--output",
        str(target),
        "--force",
        "--skip-checks",
        "--json",
    )

    assert proc.returncode == 1
    assert json.loads(proc.stdout)["error"]["code"] == "INVALID_CONFIG"
    assert victim.read_text(encoding="utf-8") == "keep me\n"


def test_json_mode_does_not_prompt_on_tty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import semantic_rails.dev_cli as dev_cli

    def fail_input(_: str = "") -> str:
        raise AssertionError("input() must not be called in --json mode")

    monkeypatch.setattr(dev_cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", fail_input)

    with pytest.raises(Exception) as exc:
        dev_cli.cmd_ask(
            argparse.Namespace(
                question=[],
                package="jaffle_shop",
                path="",
                run=False,
                compile=False,
                limit=20,
                json=True,
            )
        )
    assert getattr(exc.value, "code", "") == "INVALID_QUERY"

    dev_cli.cmd_init_project(
        argparse.Namespace(
            name="tty_json_pkg",
            package_id="",
            output="",
            workspace_root=str(tmp_path),
            description="",
            entity="event",
            relation="raw_events",
            primary_key="event_id",
            time_column="occurred_at",
            amount_column="amount",
            force=False,
            skip_checks=True,
            yes=False,
            json=True,
        )
    )
    assert (tmp_path / "tty_json_pkg" / "package.yml").is_file()


class _TTYBuffer(io.StringIO):
    @property
    def encoding(self) -> str:
        return "utf-8"

    def isatty(self) -> bool:
        return True


def _create_repl_split_package(tmp_path: Path, package_id: str) -> Path:
    import semantic_rails.dev_cli as dev_cli

    report = dev_cli.create_project_report(
        package_id=package_id,
        workspace_root=str(tmp_path),
        run_checks=False,
    )
    assert report["ok"] is True, report
    return Path(report["project_path"])


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_repl_tty_has_context_prompt_and_restrained_whimsy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import semantic_rails.dev_cli as dev_cli

    output = _TTYBuffer()
    prompts: list[str] = []
    answers = iter(["help", "exit"])

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(dev_cli.sys, "stdout", output)
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)

    dev_cli.run_interactive_shell(package="jaffle_shop")

    rendered = output.getvalue()
    assert "Semantic Rails" in rendered
    assert "Governed questions, one stop at a time." in rendered
    assert "help for the timetable" in rendered
    assert "\033[36m" in rendered
    assert "Commands" in rendered
    assert prompts == [
        "semantic-rails [jaffle_shop] › ",
        "semantic-rails [jaffle_shop] › ",
    ]
    assert all("\033[" not in prompt for prompt in prompts)


def test_repl_dumb_tty_keeps_visual_layout_without_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import semantic_rails.dev_cli as dev_cli

    output = _TTYBuffer()
    prompts: list[str] = []

    def exit_repl(prompt: str = "") -> str:
        prompts.append(prompt)
        return "exit"

    monkeypatch.setattr(dev_cli.sys, "stdout", output)
    monkeypatch.setattr("builtins.input", exit_repl)
    monkeypatch.setenv("TERM", "dumb")

    dev_cli.run_interactive_shell(package="jaffle_shop")

    rendered = output.getvalue()
    assert "╭─ Semantic Rails" in rendered
    assert "Governed questions, one stop at a time." in rendered
    assert "\033[" not in rendered
    assert prompts == ["semantic-rails [jaffle_shop] › "]


def test_repl_honors_no_color_without_losing_visual_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import semantic_rails.dev_cli as dev_cli

    output = _TTYBuffer()
    monkeypatch.setattr(dev_cli.sys, "stdout", output)
    monkeypatch.setattr("builtins.input", lambda _: "exit")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("NO_COLOR", "1")

    dev_cli.run_interactive_shell(package="jaffle_shop")

    rendered = output.getvalue()
    assert "╭─ Semantic Rails" in rendered
    assert "\033[" not in rendered


def test_repl_help_exposes_guided_authoring_undo_and_safe_validation(capsys) -> None:
    import semantic_rails.dev_cli as dev_cli

    dev_cli._print_repl_help()

    output = capsys.readouterr().out
    assert "validate [mode]" in output
    assert "Parse safely; runtime/full ask first" in output
    assert "author [kind]" in output
    assert "Create or update a semantic abstraction" in output
    assert "undo" in output
    assert "Undo the last authoring change this session" in output


def test_repl_author_refuses_non_tty_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import semantic_rails.dev_cli as dev_cli
    from semantic_rails.config_validation import PackageReference
    from semantic_rails.errors import SemanticLayerError

    project_path = _create_repl_split_package(tmp_path, "non_tty_core")
    before = _tree_bytes(project_path)
    ref = PackageReference(source_path=str(project_path))
    monkeypatch.setattr(dev_cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))

    with pytest.raises(SemanticLayerError, match="interactive terminal"):
        dev_cli._handle_repl_line("author metric", ref, undo_stack=[])

    assert _tree_bytes(project_path) == before


def test_repl_validate_parse_is_safe_while_runtime_and_full_default_to_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import semantic_rails.dev_cli as dev_cli
    from semantic_rails.config_validation import PackageReference

    project_path = _create_repl_split_package(tmp_path, "validate_core")
    ref = PackageReference(source_path=str(project_path))
    validation_modes: list[str] = []
    confirmations: list[tuple[str, bool]] = []

    def validation_report(_ref, *, mode: str, **_kwargs):
        validation_modes.append(mode)
        return {"ok": True, "summary": {}, "checks": {}}

    def confirm(label: str, *, default: bool) -> bool:
        confirmations.append((label, default))
        return default

    monkeypatch.setattr(dev_cli, "project_validation_report", validation_report)
    monkeypatch.setattr(dev_cli, "_print_project_validation", lambda _report: None)
    monkeypatch.setattr(dev_cli, "_author_confirm", confirm)

    dev_cli._handle_repl_line("validate", ref)
    dev_cli._handle_repl_line("validate runtime", ref)
    dev_cli._handle_repl_line("validate full", ref)

    output = capsys.readouterr().out
    assert validation_modes == ["parse"]
    assert confirmations == [
        ("Continue with operational validation?", False),
        ("Continue with operational validation?", False),
    ]
    assert "Safe check: parsing package YAML without warehouse queries." in output
    assert output.count("Validation cancelled") == 2


def test_repl_author_metric_previews_writes_parse_validates_and_undoes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import semantic_rails.dev_cli as dev_cli
    from semantic_rails.config_validation import PackageReference

    project_path = _create_repl_split_package(tmp_path, "guided_core")
    ref = PackageReference(source_path=str(project_path))
    before = _tree_bytes(project_path)
    prompts: list[str] = []
    answers = iter(
        [
            "gross_revenue",
            "",
            "",
            "",
            "",
            "2",
            "yes",
        ]
    )

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(dev_cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", answer)
    undo_stack = []

    dev_cli._handle_repl_line("author metric", ref, undo_stack=undo_stack)

    metric_path = project_path / "metrics" / "core" / "gross_revenue.yml"
    assert metric_path.is_file()
    metric_doc = yaml.safe_load(metric_path.read_text(encoding="utf-8"))
    assert metric_doc["metrics"]["gross_revenue"]["measure"] == ("measure.guided_core.total_amount")
    assert len(undo_stack) == 1
    assert undo_stack[0].report["parse"]["ok"] is True
    assert any(prompt.startswith("Create this metric?") for prompt in prompts)

    dev_cli._handle_repl_line("undo", ref, undo_stack=undo_stack)

    output = capsys.readouterr().out
    assert "Preview - create metric `gross_revenue`" in output
    assert "parse-validated" in output
    assert "Undid the last authoring change" in output
    assert undo_stack == []
    assert _tree_bytes(project_path) == before


def test_repl_author_segment_selects_dimension_and_basis_metric_and_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import semantic_rails.dev_cli as dev_cli
    from semantic_rails.config_validation import PackageReference

    project_path = _create_repl_split_package(tmp_path, "segment_core")
    ref = PackageReference(source_path=str(project_path))
    prompts: list[str] = []
    answers = iter(
        [
            "starter_events",
            "",
            "",
            "",
            "",
            "",
            "starter",
            "",
            "yes",
        ]
    )

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(dev_cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", answer)
    undo_stack = []

    dev_cli._handle_repl_line("author segment", ref, undo_stack=undo_stack)

    segment_path = project_path / "segments" / "core.yml"
    assert segment_path.is_file()
    segment_doc = yaml.safe_load(segment_path.read_text(encoding="utf-8"))
    segment = segment_doc["segments"]["starter_events"]
    assert segment["entity"] == "entity.segment_core_event"
    assert segment["basis_metric"] == "metric.segment_core.total_amount"
    assert segment["preview_dimensions"] == ["dimension.segment_core_event_event_type"]
    assert segment["membership"]["where"] == [
        {
            "field": "dimension.segment_core_event_event_type",
            "op": "=",
            "value": "starter",
        }
    ]
    assert len(undo_stack) == 1
    assert undo_stack[0].report["parse"]["ok"] is True
    assert any(prompt.startswith("Create this segment?") for prompt in prompts)
    output = capsys.readouterr().out
    assert "Membership dimension" in output
    assert "Metric used when previewing segment size" in output
    assert "parse-validated" in output


def test_repl_author_measure_adds_governance_meta_without_new_profile_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import semantic_rails.dev_cli as dev_cli
    from semantic_rails.config_validation import PackageReference

    project_path = _create_repl_split_package(tmp_path, "measure_core")
    ref = PackageReference(source_path=str(project_path))
    before_report = dev_cli.project_validation_report(ref, mode="parse")
    before_warnings = set(dev_cli._authoring_warning_messages(before_report))
    answers = iter(
        [
            "",
            "processing_fee",
            "",
            "",
            "",
            "fee_amount",
            "",
            "",
            "yes",
        ]
    )
    monkeypatch.setattr(dev_cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    undo_stack = []

    dev_cli._handle_repl_line("author measure", ref, undo_stack=undo_stack)

    assert len(undo_stack) == 1
    mutation = undo_stack[0]
    assert mutation.report["parse"]["ok"] is True
    model_doc = yaml.safe_load(
        (project_path / "models" / "core" / "events.yml").read_text(encoding="utf-8")
    )
    measure = model_doc["model"]["measures"]["processing_fee"]
    assert measure["meta"] == {
        "owner_team": "analytics",
        "review_priority": "medium",
        "change_risk": "medium",
    }
    after_warnings = set(dev_cli._authoring_warning_messages(mutation.report["parse"]))
    new_warnings = after_warnings - before_warnings
    governance_fields = ("owner_team", "review_priority", "change_risk")
    assert not [
        warning
        for warning in new_warnings
        if "processing_fee" in warning and any(field in warning for field in governance_fields)
    ]


def test_repl_manage_preserves_unsurfaced_fields_and_model_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import semantic_rails.dev_cli as dev_cli
    from semantic_rails.config_validation import PackageReference

    project_path = _create_repl_split_package(tmp_path, "manage_core")
    ref = PackageReference(source_path=str(project_path))
    undo_stack = []
    answers = iter(
        [
            "orders",
            "Commercial Orders",
            "raw_orders",
            "order",
            "order_id",
            "",
            "yes",
            "1",
            "event_type",
            "yes",
            "",
            "",
            "",
            "",
            "yes",
        ]
    )
    monkeypatch.setattr(dev_cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    dev_cli._handle_repl_line("author model", ref, undo_stack=undo_stack)
    dev_cli._handle_repl_line("author dimension", ref, undo_stack=undo_stack)

    orders = yaml.safe_load(
        (project_path / "models" / "core" / "orders.yml").read_text(encoding="utf-8")
    )["model"]
    events = yaml.safe_load(
        (project_path / "models" / "core" / "events.yml").read_text(encoding="utf-8")
    )["model"]
    assert orders["label"] == "Commercial Orders"
    assert events["dimensions"]["event_type"]["domain"] == ["starter", "follow_up"]
    assert undo_stack[1].report["changed_files"] == ["models/core/events.yml"]


def test_repl_manage_metric_kind_removes_stale_fields_and_preserves_public_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import semantic_rails.dev_cli as dev_cli
    from semantic_rails.config_validation import PackageReference

    project_path = _create_repl_split_package(tmp_path, "metric_manage_core")
    ref = PackageReference(source_path=str(project_path))
    answers = iter(
        [
            "total_amount",
            "yes",
            "",
            "ratio",
            "",
            "",
            "",
            "",
            "yes",
        ]
    )
    monkeypatch.setattr(dev_cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    undo_stack = []

    dev_cli._handle_repl_line("author metric", ref, undo_stack=undo_stack)

    metric = yaml.safe_load(
        (project_path / "metrics" / "core" / "starter.yml").read_text(encoding="utf-8")
    )["metrics"]["total_amount"]
    assert len(undo_stack) == 1
    assert metric["kind"] == "ratio"
    assert metric["value_type"] == "percent"
    assert metric["as"] == "metric.metric_manage_core.total_amount"
    assert metric["numerator"] == "measure.metric_manage_core.total_amount"
    assert metric["denominator"] == "measure.metric_manage_core.event_count"
    assert "measure" not in metric


def test_repl_exact_metric_key_defaults_to_cancel_without_any_file_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import semantic_rails.dev_cli as dev_cli
    from semantic_rails.config_validation import PackageReference

    project_path = _create_repl_split_package(tmp_path, "collision_core")
    ref = PackageReference(source_path=str(project_path))
    before = _tree_bytes(project_path)
    answers = iter(["total_amount", ""])
    monkeypatch.setattr(dev_cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    undo_stack = []

    dev_cli._handle_repl_line("author metric", ref, undo_stack=undo_stack)

    output = capsys.readouterr().out
    assert "`total_amount` already exists" in output
    assert "Authoring cancelled; no files changed." in output
    assert undo_stack == []
    assert _tree_bytes(project_path) == before


def test_repl_ctrl_c_cancels_only_the_wizard_and_run_still_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import semantic_rails.dev_cli as dev_cli

    project_path = _create_repl_split_package(tmp_path, "interrupt_core")
    before = _tree_bytes(project_path)
    commands = iter(["author metric", "run total amount by event type", "exit"])
    dispatched: list[tuple[str, bool]] = []

    def answer(prompt: str = "") -> str:
        if prompt.startswith("Metric key"):
            raise KeyboardInterrupt
        return next(commands)

    def ask_report(_ref, *, question: str, execute: bool = False, **_kwargs):
        dispatched.append((question, execute))
        return {"ok": True}

    monkeypatch.setattr(dev_cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(dev_cli, "ask_report", ask_report)
    monkeypatch.setattr(dev_cli, "_print_ask_report", lambda _report: print("query dispatched"))

    dev_cli.run_interactive_shell(path=str(project_path))

    output = capsys.readouterr().out
    assert "Authoring cancelled; no files changed." in output
    assert "query dispatched" in output
    assert dispatched == [("total amount by event type", True)]
    assert _tree_bytes(project_path) == before


def test_setup_human_output_is_concise_and_actionable() -> None:
    proc = _run_cli("setup", "--checks", "none")

    assert proc.returncode == 0, proc.stderr
    assert "Semantic Rails setup" in proc.stdout
    assert "registered_packages" in proc.stdout
    assert "semantic-rails init my_package" in proc.stdout
    assert "semantic-rails init --output" not in proc.stdout
