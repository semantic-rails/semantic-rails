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

import semantic_rails.repl.shell as repl_shell
from semantic_rails.config_validation import PackageReference
from semantic_rails.errors import SemanticLayerError

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(
    *args: str, env: dict[str, str] | None = None, cwd: Path = REPO_ROOT
) -> subprocess.CompletedProcess[str]:
    run_env = {**os.environ, **(env or {})}
    run_env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO_ROOT), run_env.get("PYTHONPATH")])
    )
    return subprocess.run(
        [sys.executable, "-m", "semantic_rails", *args],
        cwd=str(cwd),
        env=run_env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _run_json(*args: str, env: dict[str, str] | None = None, cwd: Path = REPO_ROOT) -> dict:
    proc = _run_cli(*args, env=env, cwd=cwd)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip(), f"CLI produced no stdout for args={args!r}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def initialized(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    """The package `init NAME --yes` creates, and init's report; the tests below only read it."""
    root = tmp_path_factory.mktemp("workspace")
    payload = _run_json("init", "init_core", "--workspace-root", str(root), "--yes", "--json")
    return root / "init_core", payload


@pytest.fixture
def client_env(tmp_path: Path) -> dict[str, str]:
    return {
        "SEMANTIC_RAILS_HOME": str(tmp_path / "semantic_rails_home"),
        "SEMANTIC_RAILS_CLAUDE_CONFIG": str(tmp_path / "Claude" / "claude_desktop_config.json"),
        "SEMANTIC_RAILS_CODEX_CONFIG": str(tmp_path / "codex" / "config.toml"),
    }


def test_project_new_and_the_setup_wizard_create_the_same_validating_package(
    tmp_path: Path, monkeypatch
) -> None:
    import semantic_rails.cli.setup_wizard as setup_wizard
    from semantic_rails.architect_scaffold import ProjectSpec, project_scaffold_files

    payload = _run_json("project", "new", "my_package", "--workspace-root", str(tmp_path), "--json")

    project_path = tmp_path / "my_package"
    assert payload["ok"] is True, payload
    assert payload["project_path"] == str(project_path)
    assert (project_path / "package.yml").is_file()
    assert (project_path / "graph.yml").is_file()
    assert {"*.duckdb", ".compiled/"} <= set(
        (project_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    )
    assert (project_path / "models" / "core" / "events.yml").is_file()
    assert payload["checks"]["parse"]["ok"] is True
    assert payload["checks"]["examples"]["passed"] == 1
    assert payload["checks"]["tests"]["passed"] == 1

    validate = _run_json(
        "project", "validate", "--path", str(project_path), "--mode", "full", "--json"
    )
    assert validate["ok"] is True, validate
    assert validate["checks"]["validate"]["ok"] is True

    def sources(root: Path) -> dict[str, bytes]:  # authored files, not build outputs
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and path.suffix != ".duckdb" and ".compiled" not in path.parts
        }

    (tmp_path / "wizard").mkdir()
    monkeypatch.chdir(tmp_path / "wizard")
    monkeypatch.setattr(setup_wizard, "_confirm", lambda *_args, **_kwargs: True)
    ref = setup_wizard._interactive_package_ref(SimpleNamespace(package="", path=""))

    wizard_package = tmp_path / "wizard" / "my_package"
    assert Path(ref.source_path) == wizard_package.resolve()
    shared = project_scaffold_files(ProjectSpec(package_id="my_package"))
    assert sources(wizard_package) == shared == sources(project_path)


def test_init_name_creates_split_package_by_default(initialized: tuple[Path, dict]) -> None:
    project_path, payload = initialized
    assert payload["ok"] is True, payload
    assert payload["project_path"] == str(project_path)
    assert (project_path / "graph.yml").is_file()
    assert (project_path / "models" / "core" / "events.yml").is_file()


def test_removed_scaffold_flags_and_mcp_commands_are_rejected(tmp_path: Path, capsys) -> None:
    from semantic_rails.cli.app import build_parser

    init = ["init", "removed_scaffold", "--workspace-root", str(tmp_path)]
    flags = ("--output", "--single-file", "--split")
    removed = [
        *(([*init, flag], f"unrecognized arguments: {flag}") for flag in flags),
        *(
            (["mcp", cmd, "--help"], "invalid choice")
            for cmd in ("http", "start", "stop", "status")
        ),
    ]
    for argv, message in removed:
        with pytest.raises(SystemExit) as exited:
            build_parser().parse_args(argv)
        assert exited.value.code == 2
        assert message in capsys.readouterr().err
    assert not (tmp_path / "removed_scaffold").exists()


def test_project_list_discovers_packages_under_extra_root(initialized: tuple[Path, dict]) -> None:
    project_path, _ = initialized
    root = str(project_path.parent)
    payload = _run_json("project", "list", "--root", root, "--with-status", "--json")
    discovered = [row for row in payload["packages"] if row["id"] == "init_core"]

    assert discovered, payload
    assert discovered[0]["origin"] == "discovered"
    assert discovered[0]["ok"] is True
    assert discovered[0]["summary"]["entities"] == 1


def test_commands_default_to_current_package_directory(initialized: tuple[Path, dict]) -> None:
    project_path, _ = initialized
    payload = _run_json("ls", "--json", cwd=project_path)

    assert payload["package"]["id"] == "init_core"
    assert payload["package"]["source_path"] == str(project_path)


def test_local_profile_can_be_initialized_and_used_as_cli_default(
    tmp_path: Path, initialized: tuple[Path, dict]
) -> None:
    env = {"SEMANTIC_RAILS_HOME": str(tmp_path / "semantic_rails_home")}
    project_path, _ = initialized

    profile = _run_json("profile", "init", "--package-path", str(project_path), "--json", env=env)

    assert profile["exists"] is True
    assert profile["resolved_package_path"] == str(project_path)
    assert Path(profile["path"]) == tmp_path / "semantic_rails_home" / "profiles.yml"

    planned = _run_json("ask", "total amount by event type", "--json", env=env)
    assert planned["ok"] is True, planned
    assert planned["package"]["source_path"] == str(project_path)


def test_local_profile_rejects_non_package_connection_modes(tmp_path: Path) -> None:
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

    with pytest.raises(SemanticLayerError) as refused:
        resolve_local_package_path(load_local_profiles(profiles_path))
    assert refused.value.code == "INVALID_CONFIG"
    assert refused.value.details == {"path": str(profiles_path), "mode": "service"}


def test_setup_without_registered_package_points_to_init(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import semantic_rails.cli.common as common
    import semantic_rails.cli.reports as reports

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SEMANTIC_RAILS_HOME", str(tmp_path / "empty_home"))
    monkeypatch.setattr(reports, "list_package_paths", lambda: {})
    monkeypatch.setattr(common, "list_package_paths", lambda: {})

    report = reports.setup_report(
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


def test_mcp_doctor_loads_path_package_and_lists_tools(initialized: tuple[Path, dict]) -> None:
    project_path, _ = initialized
    payload = _run_json("mcp", "doctor", "--path", str(project_path))

    assert payload["ok"] is True, payload
    assert payload["package"]["source_path"] == str(project_path)
    assert payload["mcp"]["tool_count"] == 5
    assert payload["mcp"]["required_tools_present"] is True
    assert any(" serve " in command for command in payload["next_commands"])
    assert any("--install --yes" in command for command in payload["next_commands"])


def test_mcp_doctor_lists_six_tools_with_segments_and_names_a_missing_one(
    runtime_factory, monkeypatch, capsys
) -> None:
    import semantic_rails.cli.commands.mcp as mcp_commands
    import semantic_rails.mcp as mcp_module

    runtime = runtime_factory("jaffle_shop")
    monkeypatch.setattr(mcp_commands, "_runtime_from_package_or_path", lambda _args: runtime)
    mcp_commands.cmd_mcp_doctor(argparse.Namespace(package="jaffle_shop", path=""))
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"], payload
    assert payload["mcp"]["tool_count"] == 6
    assert payload["mcp"]["required_tools_present"] is True

    tools = mcp_module.TOOL_DEFINITIONS
    for missing in ("discover", "inspect", "valid-values", "plan", "execute", "segment"):
        kept = tuple(tool for tool in tools if tool.name != missing)
        monkeypatch.setattr(mcp_module, "TOOL_DEFINITIONS", kept)
        result = mcp_commands._mcp_tool_check(runtime)
        assert result["required_tools_present"] is False
        assert result["missing_required_tools"] == [missing]


def test_interactive_setup_offers_foreground_serve(tmp_path: Path, monkeypatch, capsys) -> None:
    import semantic_rails.cli.setup_wizard as setup_wizard

    prompts: list[str] = []
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(
        setup_wizard,
        "_interactive_package_ref",
        lambda _args: PackageReference(source_path=str(tmp_path / "package")),
    )

    def decline(label: str, *, default: bool) -> bool:
        prompts.append(label)
        return False

    monkeypatch.setattr(setup_wizard, "_confirm", decline)
    monkeypatch.setattr(setup_wizard, "_prompt_choice", lambda *a, **k: "none")
    setup_wizard.cmd_setup_interactive(SimpleNamespace(json=False))
    output = capsys.readouterr().out

    assert not any("Start a managed" in prompt for prompt in prompts)
    assert f"semantic-rails serve --path {tmp_path / 'package'} --port 8091" in output
    assert f"semantic-rails repl --path {tmp_path / 'package'}" in output


def test_mcp_client_config_install_uses_local_files(
    initialized: tuple[Path, dict], client_env: dict[str, str]
) -> None:
    project_path, _ = initialized
    installed = _run_json(
        *("mcp", "client-config", "--path", str(project_path), "--client", "both"),
        *("--mcp", "both", "--install", "--yes"),
        env=client_env,
    )

    claude = json.loads(Path(client_env["SEMANTIC_RAILS_CLAUDE_CONFIG"]).read_text("utf-8"))
    codex = Path(client_env["SEMANTIC_RAILS_CODEX_CONFIG"]).read_text(encoding="utf-8")
    assert installed["installed"]["claude"]["ok"] is True
    assert installed["installed"]["codex"]["ok"] is True
    assert "semantic-rails" in claude["mcpServers"]
    assert "semantic-rails-architect" in claude["mcpServers"]
    assert str(project_path) in json.dumps(claude)
    assert "[mcp_servers.semantic_rails]" in codex
    assert "[mcp_servers.semantic_rails_architect]" in codex
    assert str(project_path) in codex


def test_mcp_setup_installs_client_configs_with_one_command(
    initialized: tuple[Path, dict], client_env: dict[str, str]
) -> None:
    project_path, _ = initialized
    payload = _run_json(
        *("mcp", "setup", "--path", str(project_path), "--client", "both", "--mcp", "both"),
        *("--install", "--yes", "--json"),
        env=client_env,
    )

    assert payload["ok"] is True, payload
    assert payload["mode"] == "install"
    assert payload["mcp"]["required_tools_present"] is True
    assert payload["client_config"]["installed"]["claude"]["ok"] is True
    assert payload["client_config"]["installed"]["codex"]["ok"] is True
    assert "Restart the selected MCP client" in payload["next_commands"][0]


def test_mcp_setup_uses_current_package_without_path(
    initialized: tuple[Path, dict], client_env: dict[str, str]
) -> None:
    project_path, _ = initialized
    payload = _run_json(
        *("mcp", "setup", "--client", "codex", "--mcp", "query", "--json"),
        env=client_env,
        cwd=project_path,
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

    ls = ("ls", "--package", "jaffle_shop")
    listed = _run_cli(*ls, "--resource-type", "metric", "--select", "revenue", "--limit", "3")
    assert listed.returncode == 0, listed.stderr
    assert "metric.sales.cumulative_revenue" in listed.stdout
    assert "use --limit 0 or --json" in listed.stdout
    # The REPL's `ls [kind] [search]` form lists the same objects.
    positional = _run_cli(*ls, "metric", "revenue", "--limit", "3")
    assert positional.returncode == 0 and positional.stdout == listed.stdout, positional.stderr
    searched = _run_json(*ls, "complet", "--json")
    assert (searched["resource_type"], searched["search"]) == ("all", "complet")
    every = _run_json(*ls, "--json")  # as that hint says
    assert every["truncated"] is False and len(every["objects"]) == every["count"] > 50
    times = _run_cli(*ls, "--resource-type", "time", "--limit", "2")  # `time` names roles
    assert times.returncode == 0, times.stderr
    assert "temporal_role." in times.stdout
    assert "0 time object" not in times.stdout

    refused = _run_cli("ask", "--package", "jaffle_shop", "monthly revenue by store size", "--json")
    assert refused.returncode == 1
    report = json.loads(refused.stdout)
    assert report["errors"][0]["code"] == "PLAN_UNMATCHED_TERMS"
    assert report["ok"] is False
    compiled = _run_json(
        "ask", "--package", "jaffle_shop", "monthly revenue by store name", "--compile", "--json"
    )
    assert compiled["ok"] is True, compiled
    assert compiled["plan"]["pattern"]
    assert compiled["query"]["group_by"] == ["dimension.jaffle_store_name"]
    assert "SELECT" in compiled["compile"]["sql"]


def test_adversarial_argument_validation_and_json_errors(tmp_path: Path) -> None:
    bad_root = tmp_path / "not_a_dir"
    bad_root.write_text("not a directory\n", encoding="utf-8")
    for args in (
        ("ls", "--package", "jaffle_shop", "--limit", "-1"),
        ("project", "new", "bad_root", "--workspace-root", str(bad_root), "--skip-checks"),
        ("ask", "--package", "jaffle_shop", "monthly revenue by store", "--run", "--compile"),
    ):
        proc = _run_cli(*args, "--json")
        assert proc.returncode == 1, args
        assert json.loads(proc.stdout)["error"]["code"] == "INVALID_CONFIG"


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
        "project", "new", "evil_pkg", "--output", str(target), "--force", "--skip-checks", "--json"
    )

    assert proc.returncode == 1
    assert json.loads(proc.stdout)["error"]["code"] == "INVALID_CONFIG"
    assert victim.read_text(encoding="utf-8") == "keep me\n"


def test_json_mode_does_not_prompt_on_tty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import semantic_rails.cli.commands.project as project_commands

    def fail_input(_: str = "") -> str:
        raise AssertionError("input() must not be called in --json mode")

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", fail_input)

    with pytest.raises(Exception) as exc:
        project_commands.cmd_ask(
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

    project_commands.cmd_init_project(
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


def _repl_package(tmp_path: Path, package_id: str) -> tuple[Path, PackageReference]:
    import semantic_rails.cli.scaffold as scaffold

    report = scaffold.create_project_report(
        package_id=package_id, workspace_root=str(tmp_path), run_checks=False
    )
    assert report["ok"] is True, report
    project_path = Path(report["project_path"])
    return project_path, PackageReference(source_path=str(project_path))


def _answer(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    """Answer an interactive terminal's prompts in order; returns the prompts asked."""
    prompts: list[str] = []
    replies = iter(answers)

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", answer)
    return prompts


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize(
    ("term", "no_color", "colored"),
    [("xterm-256color", False, True), ("dumb", False, False), ("xterm-256color", True, False)],
    ids=["color", "dumb-terminal", "no-color"],
)
def test_repl_tty_keeps_its_layout_and_colors_only_where_allowed(
    monkeypatch: pytest.MonkeyPatch, term: str, no_color: bool, colored: bool
) -> None:
    output = _TTYBuffer()
    prompts: list[str] = []
    answers = iter(["help", "exit"])

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setenv("TERM", term)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")

    repl_shell.run_interactive_shell(package="jaffle_shop")

    rendered = output.getvalue()
    assert "╭─ Semantic Rails" in rendered
    assert "Governed questions, one stop at a time." in rendered
    assert "help for the timetable" in rendered
    assert "Commands" in rendered
    assert ("\033[36m" in rendered) is colored
    assert colored or "\033[" not in rendered
    assert prompts == ["semantic-rails [jaffle_shop] › "] * 2  # never colored


def test_repl_help_exposes_guided_authoring_undo_and_safe_validation(capsys) -> None:
    repl_shell._print_repl_help()

    output = capsys.readouterr().out
    for command in ("validate [mode]", "author [kind]", "undo", "help [command]"):
        assert command in output


def test_repl_author_refuses_non_tty_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, ref = _repl_package(tmp_path, "non_tty_core")
    before = _tree_bytes(project_path)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))

    with pytest.raises(SemanticLayerError, match="interactive terminal"):
        repl_shell._handle_repl_line("author metric", ref, undo_stack=[])

    assert _tree_bytes(project_path) == before


def test_repl_validate_parse_is_safe_while_runtime_and_full_default_to_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _, ref = _repl_package(tmp_path, "validate_core")
    validation_modes: list[str] = []
    confirmations: list[tuple[str, bool]] = []

    def validation_report(_ref, *, mode: str, **_kwargs):
        validation_modes.append(mode)
        return {"ok": True, "summary": {}, "checks": {}}

    def confirm(label: str, *, default: bool) -> bool:
        confirmations.append((label, default))
        return default

    monkeypatch.setattr(repl_shell, "project_validation_report", validation_report)
    monkeypatch.setattr(repl_shell, "_print_project_validation", lambda _report: None)
    monkeypatch.setattr(repl_shell, "_author_confirm", confirm)

    for line in ("validate", "validate runtime", "validate full"):
        repl_shell._handle_repl_line(line, ref)

    output = capsys.readouterr().out
    assert validation_modes == ["parse"]
    assert confirmations == [("Continue with operational validation?", False)] * 2
    assert "Safe check: parsing package YAML without warehouse queries." in output
    assert output.count("Validation cancelled") == 2


def test_repl_author_metric_previews_writes_parse_validates_and_undoes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    project_path, ref = _repl_package(tmp_path, "guided_core")
    before = _tree_bytes(project_path)
    prompts = _answer(monkeypatch, ["gross_revenue", "", "", "", "2", "", "yes"])
    undo_stack = []

    repl_shell._handle_repl_line("author metric", ref, undo_stack=undo_stack)

    metric_path = project_path / "metrics" / "core" / "gross_revenue.yml"
    metric_doc = yaml.safe_load(metric_path.read_text(encoding="utf-8"))
    assert metric_doc["metrics"]["gross_revenue"]["measure"] == "measure.guided_core.total_amount"
    assert len(undo_stack) == 1
    assert undo_stack[0].report["parse"]["ok"] is True
    assert any(prompt.startswith("Create this metric?") for prompt in prompts)

    repl_shell._handle_repl_line("undo", ref, undo_stack=undo_stack)

    output = capsys.readouterr().out
    assert "Preview - create metric `gross_revenue`" in output
    assert "parse-validated" in output
    assert "Undid the last authoring change" in output
    assert undo_stack == []
    assert _tree_bytes(project_path) == before


def test_repl_author_segment_selects_dimension_and_basis_metric_and_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    project_path, ref = _repl_package(tmp_path, "segment_core")
    answers = ["starter_events", "", "", "", "", "", "starter", "", "yes"]
    prompts = _answer(monkeypatch, answers)
    undo_stack = []

    repl_shell._handle_repl_line("author segment", ref, undo_stack=undo_stack)

    segment_doc = yaml.safe_load((project_path / "segments" / "core.yml").read_text("utf-8"))
    segment = segment_doc["segments"]["starter_events"]
    assert segment["entity"] == "entity.segment_core_event"
    assert segment["basis_metric"] == "metric.segment_core.event_count"
    assert segment["preview_dimensions"] == ["dimension.segment_core_event_event_type"]
    assert segment["membership"]["where"] == [
        {"field": "dimension.segment_core_event_event_type", "op": "=", "value": "starter"}
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
    import semantic_rails.cli.output as cli_output
    import semantic_rails.cli.reports as reports

    project_path, ref = _repl_package(tmp_path, "measure_core")
    before_report = reports.project_validation_report(ref, mode="parse")
    before_warnings = set(cli_output._authoring_warning_messages(before_report))
    _answer(monkeypatch, ["", "processing_fee", "", "", "", "fee_amount", "", "", "yes"])
    undo_stack = []

    repl_shell._handle_repl_line("author measure", ref, undo_stack=undo_stack)

    assert len(undo_stack) == 1
    mutation = undo_stack[0]
    assert mutation.report["parse"]["ok"] is True
    model_doc = yaml.safe_load(
        (project_path / "models" / "core" / "events.yml").read_text(encoding="utf-8")
    )
    measure = model_doc["model"]["measures"]["processing_fee"]
    meta = {"owner_team": "analytics", "review_priority": "medium", "change_risk": "medium"}
    assert measure["meta"] == meta
    after_warnings = set(cli_output._authoring_warning_messages(mutation.report["parse"]))
    assert not [
        warning
        for warning in after_warnings - before_warnings
        if "processing_fee" in warning and any(field in warning for field in meta)
    ]


def test_repl_manage_preserves_unsurfaced_fields_and_model_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, ref = _repl_package(tmp_path, "manage_core")
    events_path = project_path / "models" / "core" / "events.yml"
    starter = yaml.safe_load(events_path.read_text(encoding="utf-8"))
    starter["model"]["dimensions"]["event_type"]["domain"] = ["starter", "follow_up"]
    events_path.write_text(yaml.safe_dump(starter, sort_keys=False), encoding="utf-8")
    undo_stack = []
    model = ["orders", "Commercial Orders", "raw_orders", "order", "order_id", "", "yes"]
    _answer(monkeypatch, [*model, "1", "event_type", "yes", "", "", "", "", "yes"])

    repl_shell._handle_repl_line("author model", ref, undo_stack=undo_stack)
    repl_shell._handle_repl_line("author dimension", ref, undo_stack=undo_stack)

    orders = yaml.safe_load((project_path / "models" / "core" / "orders.yml").read_text("utf-8"))
    events = yaml.safe_load(events_path.read_text(encoding="utf-8"))
    assert orders["model"]["label"] == "Commercial Orders"
    assert events["model"]["dimensions"]["event_type"]["domain"] == ["starter", "follow_up"]
    assert undo_stack[1].report["changed_files"] == ["models/core/events.yml"]


def test_repl_manage_metric_kind_removes_stale_fields_and_preserves_public_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, ref = _repl_package(tmp_path, "metric_manage_core")
    _answer(monkeypatch, ["total_amount", "yes", "", "ratio", "", "", "", "", "yes"])
    undo_stack = []

    repl_shell._handle_repl_line("author metric", ref, undo_stack=undo_stack)

    metrics = yaml.safe_load((project_path / "metrics" / "core.yml").read_text(encoding="utf-8"))
    metric = metrics["metrics"]["total_amount"]
    assert len(undo_stack) == 1
    assert metric["kind"] == "ratio"
    assert metric["value_type"] == "ratio"  # an amount per event, not a percent
    assert metric["as"] == "metric.metric_manage_core.total_amount"
    assert metric["numerator"] == "measure.metric_manage_core.total_amount"
    assert metric["denominator"] == "measure.metric_manage_core.event_count"
    assert "measure" not in metric


def test_repl_exact_metric_key_asks_for_another_key_without_any_file_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    project_path, ref = _repl_package(tmp_path, "collision_core")
    before = _tree_bytes(project_path)
    _answer(monkeypatch, ["total_amount", "", "cancel"])  # Enter: don't update it
    undo_stack = []

    repl_shell._handle_repl_line("author metric", ref, undo_stack=undo_stack)

    output = capsys.readouterr().out
    assert "`total_amount` already exists" in output
    assert "Enter another metric key, or type cancel." in output
    assert "Authoring cancelled; no files changed." in output
    assert undo_stack == []
    assert _tree_bytes(project_path) == before


def test_repl_ctrl_c_cancels_only_the_wizard_and_run_still_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    project_path, _ = _repl_package(tmp_path, "interrupt_core")
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

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(repl_shell, "ask_report", ask_report)
    monkeypatch.setattr(repl_shell, "_print_ask_report", lambda _report: print("query dispatched"))

    repl_shell.run_interactive_shell(path=str(project_path))

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
