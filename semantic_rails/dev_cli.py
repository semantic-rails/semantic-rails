"""Primary human-facing commands for the ``semantic-rails`` CLI.

These commands are the developer product surface: interactive setup,
dbt-style debugging and listing, natural-language planning, split-package
scaffolding, and a lightweight command loop. The JSON-first runtime
commands still exist for automation and API parity, but they are not the
shape developers need to live in during authoring.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shlex
import shutil
import sys
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml

from .architect_service import ArchitectMutation, ArchitectProject
from .catalog_service import resolve_catalog
from .config import (
    list_package_paths,
    package_root_for_source,
    repo_root,
)
from .config_validation import (
    PackageReference,
    parse_config_report,
    resolve_package_reference,
    validate_config_report,
)
from .errors import SemanticLayerError
from .local_config import (
    init_local_profile,
    local_profile_report,
    resolve_local_package_path,
)
from .mcp_manager import (
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORT,
    managed_mcp_lifecycle_report,
    mcp_client_config_report,
    start_mcp_http_server,
    stop_mcp_http_server,
)
from .package_tools import (
    check_package_report,
    run_examples_report,
    run_package_tests_report,
)
from .planner import plan_payload
from .runtime import Runtime

PROJECT_CHECK_MODES = ("parse", "runtime", "examples", "tests", "full")
CATALOG_KINDS = ("all", "entity", "dimension", "measure", "metric", "segment", "time")
_EXCLUDED_DISCOVERY_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}


def add_developer_cli(sub: argparse._SubParsersAction, package_choices: list[str]) -> None:
    """Register human-facing developer commands on the main parser."""

    p_setup = sub.add_parser(
        "setup",
        description=(
            "Check the local Semantic Rails developer setup and print useful first commands."
        ),
    )
    _add_optional_reference_args(p_setup, package_choices)
    p_setup.add_argument(
        "--checks",
        choices=["none", "parse", "runtime", "full"],
        default="parse",
        help="Optional package checks to run during setup (default: parse).",
    )
    p_setup.add_argument(
        "--server",
        action="store_true",
        help="Also check the optional HTTP server dependency.",
    )
    p_setup.add_argument(
        "--interactive",
        action="store_true",
        help="Run the guided first-run setup wizard.",
    )
    p_setup.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_setup.set_defaults(func=cmd_setup, human_cli=True)

    p_debug = sub.add_parser(
        "debug",
        description="Run dbt-style local environment, dependency, package, and warehouse checks.",
    )
    _add_optional_reference_args(p_debug, package_choices)
    p_debug.add_argument(
        "--checks",
        choices=["none", "parse", "runtime", "full"],
        default="runtime",
        help="Package checks to run (default: runtime).",
    )
    p_debug.add_argument(
        "--server",
        action="store_true",
        help="Also check the optional HTTP server dependency.",
    )
    p_debug.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_debug.set_defaults(func=cmd_debug, human_cli=True)

    p_ls = sub.add_parser(
        "ls",
        description="List semantic objects in the active package, similar to dbt ls.",
    )
    _add_optional_reference_args(p_ls, package_choices)
    p_ls.add_argument(
        "--resource-type",
        "--kind",
        choices=CATALOG_KINDS,
        default="all",
        help="Object type to list (default: all).",
    )
    p_ls.add_argument(
        "--select",
        "--search",
        dest="search",
        default="",
        help="Substring search over object ids, labels, and names.",
    )
    p_ls.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum objects to print in human output (default: 50).",
    )
    p_ls.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_ls.set_defaults(func=cmd_ls, human_cli=True)

    p_ask = sub.add_parser(
        "ask",
        description="Plan a Semantic Rails query from a natural-language question.",
    )
    _add_optional_reference_args(p_ask, package_choices)
    p_ask.add_argument("question", nargs="*", help="Question or intent to plan.")
    p_ask.add_argument(
        "--run",
        "--execute",
        action="store_true",
        help="Execute the planned query after validation.",
    )
    p_ask.add_argument(
        "--compile",
        action="store_true",
        help="Compile the planned query and print SQL instead of executing it. Mutually exclusive with --run.",
    )
    p_ask.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Row limit to apply when --run is used (default: 20).",
    )
    p_ask.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_ask.set_defaults(func=cmd_ask, human_cli=True)

    p_repl = sub.add_parser(
        "repl",
        description="Open an interactive Semantic Rails command loop.",
    )
    _add_optional_reference_args(p_repl, package_choices)
    p_repl.set_defaults(func=cmd_repl, human_cli=True)

    p_profile = sub.add_parser(
        "profile",
        description=(
            "Manage optional local CLI defaults in ~/.semantic_rails/profiles.yml. "
            "Package files stay portable; profiles are machine-local."
        ),
    )
    profile_sub = p_profile.add_subparsers(dest="profile_cmd", required=True)

    p_profile_init = profile_sub.add_parser(
        "init",
        description="Create or update a local default package profile.",
    )
    p_profile_init.add_argument(
        "--package-path",
        "--path",
        required=True,
        help="Package directory or single-file package YAML to use as the local default.",
    )
    p_profile_init.add_argument(
        "--profile",
        default="local",
        help="Local profile name to activate (default: local).",
    )
    p_profile_init.add_argument(
        "--target",
        default="dev",
        help="Local target name to activate (default: dev).",
    )
    p_profile_init.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_profile_init.set_defaults(func=cmd_profile_init, human_cli=True)

    p_profile_show = profile_sub.add_parser(
        "show",
        description="Show the active local Semantic Rails profile.",
    )
    p_profile_show.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_profile_show.set_defaults(func=cmd_profile_show, human_cli=True)

    p_profile_list = profile_sub.add_parser(
        "list",
        description="List local Semantic Rails profile names.",
    )
    p_profile_list.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_profile_list.set_defaults(func=cmd_profile_show, human_cli=True)

    p_project = sub.add_parser(
        "project",
        description="Human developer workflows for Semantic Rails package projects.",
    )
    project_sub = p_project.add_subparsers(dest="project_cmd", required=True)

    p_project_list = project_sub.add_parser(
        "list",
        description="List registered packages and optionally discover packages under extra roots.",
    )
    p_project_list.add_argument(
        "--root",
        action="append",
        default=[],
        help="Additional workspace root to scan for package.yml files. May be repeated.",
    )
    p_project_list.add_argument(
        "--with-status",
        action="store_true",
        help="Parse each package and include health summaries.",
    )
    p_project_list.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_project_list.set_defaults(func=cmd_project_list, human_cli=True)

    p_project_new = project_sub.add_parser(
        "new",
        description="Create a runnable split-layout package for development.",
    )
    p_project_new.add_argument("package_id", help="Package id and directory name to create.")
    p_project_new.add_argument(
        "--output",
        "-o",
        default="",
        help=(
            "Target package directory. Defaults to configs/semantic_rails/<package-id> "
            "when that root exists, otherwise ./<package-id>."
        ),
    )
    p_project_new.add_argument(
        "--workspace-root",
        default="",
        help="Base directory used when choosing the default output path.",
    )
    p_project_new.add_argument(
        "--description",
        default="",
        help="Package description to write into package.yml.",
    )
    p_project_new.add_argument(
        "--entity",
        default="event",
        help="Starter business entity key (default: event).",
    )
    p_project_new.add_argument(
        "--relation",
        default="raw_events",
        help="Starter warehouse relation and CSV filename stem (default: raw_events).",
    )
    p_project_new.add_argument(
        "--primary-key",
        default="event_id",
        help="Primary key column for the starter entity (default: event_id).",
    )
    p_project_new.add_argument(
        "--time-column",
        default="occurred_at",
        help="Timestamp column for the starter metric (default: occurred_at).",
    )
    p_project_new.add_argument(
        "--amount-column",
        default="amount",
        help="Numeric column for the starter flow measure (default: amount).",
    )
    p_project_new.add_argument(
        "--force",
        action="store_true",
        help="Overwrite starter files in an existing non-empty target directory.",
    )
    p_project_new.add_argument(
        "--skip-checks",
        action="store_true",
        help="Create files without running parse/runtime/examples/tests checks.",
    )
    p_project_new.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_project_new.set_defaults(func=cmd_project_new, human_cli=True)

    p_project_status = project_sub.add_parser(
        "status",
        description="Summarize a package project and optionally run validation checks.",
    )
    _add_optional_reference_args(p_project_status, package_choices)
    p_project_status.add_argument(
        "--checks",
        choices=PROJECT_CHECK_MODES,
        default="parse",
        help="Checks to run (default: parse).",
    )
    p_project_status.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_project_status.set_defaults(func=cmd_project_status, human_cli=True)

    p_project_validate = project_sub.add_parser(
        "validate",
        description="Run a selected validation mode against a package project.",
    )
    _add_optional_reference_args(p_project_validate, package_choices)
    p_project_validate.add_argument(
        "--mode",
        choices=PROJECT_CHECK_MODES,
        default="full",
        help="Validation mode to run (default: full).",
    )
    p_project_validate.add_argument(
        "--compare-path",
        default="",
        help="Optional baseline package path for full checks.",
    )
    p_project_validate.add_argument(
        "--base-ref",
        default="",
        help="Optional git ref to use as the baseline when --compare-path is not set.",
    )
    p_project_validate.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_project_validate.set_defaults(func=cmd_project_validate, human_cli=True)


def cmd_setup(args: argparse.Namespace) -> None:
    if getattr(args, "interactive", False):
        cmd_setup_interactive(args)
        return
    report = setup_report(args)
    if args.json:
        _print_json(report)
    else:
        _print_setup_report(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_setup_interactive(args: argparse.Namespace) -> None:
    if getattr(args, "json", False):
        raise SemanticLayerError("INVALID_CONFIG", "--interactive cannot be combined with --json")
    if not sys.stdin.isatty():
        raise SemanticLayerError("INVALID_CONFIG", "--interactive requires a terminal")

    print("Semantic Rails setup wizard")
    print()
    ref = _interactive_package_ref(args)

    if _confirm("Set this package as the local CLI default?", default=True):
        profile_report = init_local_profile(package_path=ref.source_path)
        print(f"Local profile: {profile_report['path']}")

    if _confirm("Run full package validation now?", default=True):
        validation = project_validation_report(ref, mode="full")
        _print_project_validation(validation)
        if not validation.get("ok"):
            raise SystemExit(1)

    lifecycle = managed_mcp_lifecycle_report()
    if lifecycle["supported"]:
        if _confirm("Start a managed local MCP HTTP server now?", default=False):
            _start_managed_mcp_from_wizard(ref)
    else:
        print(
            "Managed background MCP start/stop is POSIX-only. On Windows, install "
            "the generated client config so Claude/Codex launches stdio, or run "
            "`semantic-rails mcp http` in a foreground terminal."
        )

    client = _prompt_choice(
        "Install local MCP config into Claude/Codex?",
        choices=["none", "claude", "codex", "both"],
        default="none",
    )
    if client != "none":
        include_architect = _confirm("Include Architect MCP for package authoring?", default=True)
        preview = mcp_client_config_report(
            ref,
            client=client,
            mcp="both" if include_architect else "query",
            install=False,
        )
        print("Config files to update:")
        for selected, payload in dict(preview.get("previews", {}) or {}).items():
            print(f"  {selected}: {payload.get('path')}")
        if _confirm("Write these MCP client config files?", default=False):
            config = mcp_client_config_report(
                ref,
                client=client,
                mcp="both" if include_architect else "query",
                install=True,
            )
            _print_json(config["installed"])
            print("Restart the selected MCP client so it reloads its config.")

    print()
    print("Next commands:")
    for command in [
        f"semantic-rails repl --path {_quote(ref.source_path)}",
        f"semantic-rails project status --path {_quote(ref.source_path)}",
        f'semantic-rails ask --path {_quote(ref.source_path)} "total amount by event type" --run',
        f"semantic-rails mcp setup --path {_quote(ref.source_path)}",
        "semantic-rails mcp status",
    ]:
        print(f"  {command}")


def _start_managed_mcp_from_wizard(ref: PackageReference) -> None:
    """Start the optional local server without turning a conflict into setup failure."""

    try:
        start = start_mcp_http_server(
            ref,
            name="default",
            host=DEFAULT_MCP_HOST,
            port=DEFAULT_MCP_PORT,
        )
    except SemanticLayerError as exc:
        server = dict(exc.details.get("server", {}) or {})
        stale = exc.code == "CONFIG_CONFLICT" and not server.get("pid_alive", True)
        if stale:
            print("A dead 'default' MCP registration is blocking startup.")
            if _confirm("Remove the stale registration and retry?", default=True):
                cleanup = stop_mcp_http_server(name="default")
                print(f"Stale registration: {cleanup.get('status', 'removed')}")
                start = start_mcp_http_server(
                    ref,
                    name="default",
                    host=DEFAULT_MCP_HOST,
                    port=DEFAULT_MCP_PORT,
                )
            else:
                print("MCP server not started. Setup will continue.")
                print("  semantic-rails mcp stop --name default")
                return
        elif exc.code == "CONFIG_CONFLICT":
            print(f"MCP server not started: {exc}")
            print("Setup will continue; inspect it with `semantic-rails mcp status`.")
            print("Stop the named server explicitly before replacing it.")
            return
        else:
            raise

    _print_json(start)
    if not start.get("ok"):
        print("Managed MCP did not become healthy. Setup will continue.")
        print("  semantic-rails mcp status")
        print("  semantic-rails mcp stop --name default")


def _interactive_package_ref(args: argparse.Namespace) -> PackageReference:
    explicit = bool(
        str(getattr(args, "package", "") or "").strip()
        or str(getattr(args, "path", "") or "").strip()
    )
    if explicit:
        return _ref_from_args(args, allow_default=False)

    cwd_ref = _package_ref_from_cwd()
    if cwd_ref is not None:
        print(f"Using package from current directory: {_ref_label(cwd_ref)}")
        return cwd_ref

    default_path = Path.cwd() / "my_package"
    if (default_path / "package.yml").is_file():
        ref = PackageReference(source_path=str(default_path.resolve()))
        print(f"Using existing package: {ref.source_path}")
        return ref

    if _confirm(f"Create a starter package at {default_path}?", default=True):
        report = create_project_report(
            package_id=default_path.name,
            output=str(default_path),
            run_checks=True,
        )
        _print_project_created(report)
        if not report.get("ok"):
            raise SystemExit(1)
        return PackageReference(source_path=str(default_path.resolve()))

    package_path = _prompt("Package path", str(default_path))
    return resolve_package_reference(path=package_path)


def cmd_debug(args: argparse.Namespace) -> None:
    report = setup_report(args)
    report["command"] = "debug"
    if args.json:
        _print_json(report)
    else:
        _print_debug_report(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_ls(args: argparse.Namespace) -> None:
    ref = _ref_from_args(args)
    report = list_objects_report(
        ref,
        resource_type=args.resource_type,
        search=args.search,
        limit=args.limit,
    )
    if args.json:
        _print_json(report)
    else:
        _print_objects(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_ask(args: argparse.Namespace) -> None:
    question = " ".join(args.question).strip()
    if args.run and args.compile:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Choose either --run or --compile, not both.",
        )
    if not question and sys.stdin.isatty() and not args.json:
        question = input("Question: ").strip()
    if not question:
        raise SemanticLayerError(
            "INVALID_QUERY",
            "Provide a question, for example: semantic-rails ask 'monthly revenue by store'",
            details={"path": "question"},
        )
    ref = _ref_from_args(args)
    report = ask_report(
        ref,
        question=question,
        execute=args.run,
        compile_sql=args.compile,
        limit=args.limit,
    )
    if args.json:
        _print_json(report)
    else:
        _print_ask_report(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_repl(args: argparse.Namespace) -> None:
    run_interactive_shell(package=getattr(args, "package", ""), path=getattr(args, "path", ""))


def cmd_project_list(args: argparse.Namespace) -> None:
    report = project_list_report(roots=args.root, with_status=args.with_status)
    if args.json:
        _print_json(report)
    else:
        _print_project_list(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_project_new(args: argparse.Namespace) -> None:
    report = create_project_report(
        package_id=args.package_id,
        output=args.output,
        workspace_root=args.workspace_root,
        description=args.description,
        entity=args.entity,
        relation=args.relation,
        primary_key=args.primary_key,
        time_column=args.time_column,
        amount_column=args.amount_column,
        force=args.force,
        run_checks=not args.skip_checks,
    )
    if args.json:
        _print_json(report)
    else:
        _print_project_created(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_init_project(args: argparse.Namespace) -> None:
    package_id = str(getattr(args, "name", "") or getattr(args, "package_id", "") or "").strip()
    if not package_id and sys.stdin.isatty():
        package_id = input("Package name: ").strip()
    if not package_id:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Provide a package name, for example: semantic-rails init analytics_core",
        )
    interactive = (
        sys.stdin.isatty() and not getattr(args, "yes", False) and not getattr(args, "json", False)
    )
    description = str(getattr(args, "description", "") or "")
    entity = str(getattr(args, "entity", "") or "event")
    relation = str(getattr(args, "relation", "") or "raw_events")
    primary_key = str(getattr(args, "primary_key", "") or "event_id")
    time_column = str(getattr(args, "time_column", "") or "occurred_at")
    amount_column = str(getattr(args, "amount_column", "") or "amount")
    output = str(getattr(args, "output", "") or "")
    if interactive:
        description = _prompt(
            "Description", description or f"{_title(package_id)} analytics package."
        )
        entity = _prompt("First entity", entity)
        relation = _prompt("Starter relation/table", relation)
        primary_key = _prompt("Primary key column", primary_key)
        time_column = _prompt("Time column", time_column)
        amount_column = _prompt("Amount column", amount_column)
        output = _prompt(
            "Output directory",
            output
            or str(
                _project_target(
                    _slug(package_id), output="", workspace_root=getattr(args, "workspace_root", "")
                )
            ),
        )
    report = create_project_report(
        package_id=package_id,
        output=output,
        workspace_root=str(getattr(args, "workspace_root", "") or ""),
        description=description,
        entity=entity,
        relation=relation,
        primary_key=primary_key,
        time_column=time_column,
        amount_column=amount_column,
        force=bool(getattr(args, "force", False)),
        run_checks=not bool(getattr(args, "skip_checks", False)),
    )
    if getattr(args, "json", False):
        _print_json(report)
    else:
        _print_project_created(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_project_status(args: argparse.Namespace) -> None:
    ref = _ref_from_args(args)
    report = project_status_report(ref, checks=args.checks)
    if args.json:
        _print_json(report)
    else:
        _print_project_status(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_project_validate(args: argparse.Namespace) -> None:
    ref = _ref_from_args(args)
    report = project_validation_report(
        ref,
        mode=args.mode,
        compare_path=args.compare_path,
        base_ref=args.base_ref,
    )
    if args.json:
        _print_json(report)
    else:
        _print_project_validation(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_profile_init(args: argparse.Namespace) -> None:
    report = init_local_profile(
        package_path=args.package_path,
        profile=args.profile,
        target=args.target,
    )
    if args.json:
        _print_json(report)
    else:
        _print_profile_report(report)


def cmd_profile_show(args: argparse.Namespace) -> None:
    report = local_profile_report()
    if args.json:
        _print_json(report)
    else:
        _print_profile_report(report)


def setup_report(args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    checks.append(_python_check())
    checks.append(_module_check("duckdb", package="duckdb"))
    checks.append(_module_check("yaml", package="PyYAML"))
    if args.server:
        checks.append(_module_check("uvicorn", package="uvicorn", optional=True))
    checks.append(_uv_check())

    packages = _registered_package_rows(with_status=False)
    checks.append(
        {
            "name": "registered_packages",
            "ok": True,
            "required": False,
            "summary": f"{len(packages)} package(s)",
            "packages": [row["id"] for row in packages],
        }
    )
    profile = local_profile_report()
    checks.append(
        {
            "name": "local_profile",
            "ok": True,
            "required": False,
            "summary": profile["package_path"] or "not configured",
            "path": profile["path"],
        }
    )

    package_check: dict[str, Any] | None = None
    if args.checks != "none":
        explicit_ref = bool(
            str(getattr(args, "package", "") or "").strip()
            or str(getattr(args, "path", "") or "").strip()
        )
        try:
            ref = _ref_from_args(args, allow_default=True)
        except SemanticLayerError as exc:
            if explicit_ref or "Local Semantic Rails profile" in str(exc):
                raise
            package_check = {
                "name": f"package_{args.checks}",
                "ok": True,
                "required": False,
                "summary": "no package selected",
                "message": "Run semantic-rails init my_package, then re-run setup with --path.",
            }
        else:
            if args.checks == "parse":
                package_check = _parse_check(ref)
            elif args.checks == "runtime":
                package_check = _runtime_check(ref)
            else:
                package_check = _full_check(ref)
            package_check["name"] = f"package_{args.checks}"
            package_check["required"] = True
        if package_check:
            checks.append(package_check)

    ok = all(bool(check.get("ok")) for check in checks if check.get("required", True))
    return {
        "ok": ok,
        "repo_root": repo_root(),
        "python": sys.version.split()[0],
        "checks": checks,
        "packages": packages,
        "local_profile": profile,
        "next_actions": _setup_next_actions(packages),
    }


def project_list_report(*, roots: Iterable[str] = (), with_status: bool = False) -> dict[str, Any]:
    rows = _registered_package_rows(with_status=with_status)
    seen = {str(Path(row["source_path"]).resolve()) for row in rows}

    for root in roots:
        for source in _discover_package_sources(Path(root).expanduser().resolve()):
            resolved = str(Path(source).resolve())
            if resolved in seen:
                continue
            row = _package_row_from_source(source, origin="discovered", with_status=with_status)
            rows.append(row)
            seen.add(resolved)

    rows.sort(key=lambda row: (str(row.get("id", "")), str(row.get("source_path", ""))))
    return {
        "ok": all(bool(row.get("ok", True)) for row in rows),
        "count": len(rows),
        "packages": rows,
    }


def list_objects_report(
    ref: PackageReference,
    *,
    resource_type: str = "all",
    search: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    if limit < 0:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "--limit must be greater than or equal to 0",
            details={"limit": limit},
        )
    runtime = _runtime_from_ref(ref)
    try:
        selected = _catalog_kind(resource_type)
        catalog = resolve_catalog(
            runtime,
            view="summary",
            verbosity="compact",
            kind=_runtime_catalog_kind(selected),
            search=search,
        )
        objects = _catalog_objects(catalog, selected=selected)
        return {
            "ok": True,
            "package": _runtime_package_payload(runtime, ref),
            "resource_type": selected,
            "search": search,
            "count": len(objects),
            "objects": objects[: max(0, limit)] if limit else objects,
            "truncated": bool(limit and len(objects) > limit),
            "catalog_counts": dict(catalog.get("counts", {}) or {}),
        }
    finally:
        runtime.close()


def ask_report(
    ref: PackageReference,
    *,
    question: str,
    execute: bool = False,
    compile_sql: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    if limit < 0:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "--limit must be greater than or equal to 0",
            details={"limit": limit},
        )
    runtime = _runtime_from_ref(ref)
    try:
        plan = plan_payload(runtime, intent=question, partial_query=None, limit=3, detail="best")
        query = _planned_query(plan)
        out: dict[str, Any] = {
            "ok": _payload_ok(plan) and bool(query),
            "package": _runtime_package_payload(runtime, ref),
            "question": question,
            "plan": _compact_plan(plan),
            "query": query,
        }
        if not _payload_ok(plan):
            out["errors"] = list(plan.get("errors", []) or [])
            return out
        if not query:
            out["ok"] = False
            out["errors"] = [{"code": "NO_PLAN", "message": "No executable query was planned"}]
            return out
        if execute:
            executable_query = dict(query)
            if limit and "limit" not in executable_query:
                executable_query["limit"] = limit
            result = runtime.query(executable_query)
            out["result"] = {
                "ok": bool(result.get("ok", True)),
                "rows": list(result.get("rows", []) or []),
                "row_count": result.get("row_count", len(result.get("rows", []) or [])),
                "output_columns": list(result.get("output_columns", []) or []),
            }
            out["ok"] = out["ok"] and bool(out["result"]["ok"])
        if compile_sql:
            compiled = runtime.compile(query)
            out["compile"] = {
                "ok": bool(compiled.get("ok", True)),
                "sql": compiled.get("rendered_sql", compiled.get("sql", "")),
                "dialect": compiled.get("dialect", runtime.warehouse),
                "output_columns": list(compiled.get("output_columns", []) or []),
            }
            out["ok"] = out["ok"] and bool(out["compile"]["ok"])
        return out
    finally:
        runtime.close()


def create_project_report(
    *,
    package_id: str,
    output: str = "",
    workspace_root: str = "",
    description: str = "",
    entity: str = "event",
    relation: str = "raw_events",
    primary_key: str = "event_id",
    time_column: str = "occurred_at",
    amount_column: str = "amount",
    force: bool = False,
    run_checks: bool = True,
) -> dict[str, Any]:
    package_slug = _slug(package_id)
    target = _project_target(package_slug, output=output, workspace_root=workspace_root)
    _validate_project_target(target)
    if target.name != package_slug:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Directory package paths must end with the package id",
            details={"package_id": package_slug, "target": str(target)},
        )
    if target.exists() and any(target.iterdir()) and not force:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Target directory '{target}' is not empty; pass --force to overwrite starter files",
            details={"target": str(target)},
        )
    _reject_symlink_tree(target)

    entity_slug = _slug(entity)
    relation_slug = _slug(relation, fallback="raw_events")
    primary_key_slug = _slug(primary_key, fallback="event_id")
    time_column_slug = _slug(time_column, fallback="occurred_at")
    amount_column_slug = _slug(amount_column, fallback="amount")
    documents = _starter_documents(
        package_id=package_slug,
        description=description or f"{_title(package_slug)} Semantic Rails package.",
        entity=entity_slug,
        relation=relation_slug,
        primary_key=primary_key_slug,
        time_column=time_column_slug,
        amount_column=amount_column_slug,
    )

    changed: list[str] = []
    for relative, payload in documents["yaml"].items():
        path = target / relative
        _write_project_text(
            target,
            path,
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        )
        changed.append(relative)

    for relative, content in documents["text"].items():
        _write_project_text(target, target / relative, content)
        changed.append(relative)

    csv_path = target / "data" / f"{package_slug}_csv" / f"{relation_slug}.csv"
    _write_project_text(target, csv_path, documents["csv"])
    changed.append(csv_path.relative_to(target).as_posix())

    ref = PackageReference(source_path=str(target))
    checks = (
        project_validation_report(ref, mode="full") if run_checks else {"ok": True, "checks": {}}
    )
    ok = bool(checks.get("ok", False))
    return {
        "ok": ok,
        "status": "created" if ok else "created_with_check_failures",
        "package_id": package_slug,
        "project_path": str(target),
        "changed_files": changed,
        "checks": checks.get("checks", {}),
        "summary": checks.get("summary", {}),
        "example_query_path": "examples/core.yml",
        "next_actions": [
            f"semantic-rails project status --path {_quote(target)}",
            f"semantic-rails project validate --path {_quote(target)}",
            f"semantic-rails profile init --package-path {_quote(target)}",
            f'semantic-rails ask --path {_quote(target)} "total amount by event type" --run',
            f"semantic-rails mcp setup --path {_quote(target)}",
        ],
    }


def run_interactive_shell(*, package: str = "", path: str = "") -> None:
    current_ref = _default_ref(package=package, path=path)
    undo_stack: list[ArchitectMutation] = []
    _print_repl_welcome(current_ref)
    while True:
        try:
            line = input(_repl_prompt(current_ref)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in {"exit", "quit", ":q"}:
            return
        if line == "help":
            _print_repl_help()
            continue
        try:
            current_ref = _handle_repl_line(line, current_ref, undo_stack=undo_stack)
        except SemanticLayerError as exc:
            print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        except Exception as exc:  # pragma: no cover - defensive REPL guard
            print(f"error [INTERNAL_ERROR]: {exc}", file=sys.stderr)


def _print_repl_welcome(current_ref: PackageReference) -> None:
    visual, color = _repl_capabilities()
    if not visual:
        print("Semantic Rails interactive")
        print("Type help for commands, exit to quit.")
        print(f"Using {_ref_label(current_ref)}")
        return

    def accent(text: str) -> str:
        return _repl_color(text, "36", enabled=color)

    title = "Semantic Rails"
    tagline = "Governed questions, one stop at a time."
    inner_width = 46
    title_rule = f"─ {title} ".ljust(inner_width, "─")
    middle = f"  {tagline}".ljust(inner_width)

    print()
    print(accent(f"╭{title_rule}╮"))
    print(f"{accent('│')}{middle}{accent('│')}")
    print(accent(f"╰{'─' * inner_width}╯"))
    print(f"  package  {_ref_label(current_ref)}")
    print("  help     type help for the timetable · exit when done")
    print()


def _repl_prompt(current_ref: PackageReference) -> str:
    visual, _ = _repl_capabilities()
    if not visual:
        return "semantic-rails> "
    label = current_ref.package_id or Path(current_ref.source_path).name or current_ref.source_path
    return f"semantic-rails [{label}] › "


def _repl_capabilities(stream: Any | None = None) -> tuple[bool, bool]:
    stream = stream or sys.stdout
    try:
        is_terminal = bool(stream.isatty())
    except (AttributeError, OSError):
        is_terminal = False
    if not is_terminal:
        return False, False

    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "╭─›·".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False, False

    color = (
        os.environ.get("TERM", "").lower() != "dumb"
        and "NO_COLOR" not in os.environ
        and os.environ.get("CLICOLOR") != "0"
    )
    return True, color


def _repl_color(text: str, code: str, *, enabled: bool) -> str:
    if not enabled:
        return text
    return f"\033[{code}m{text}\033[0m"


def _handle_repl_line(
    line: str,
    current_ref: PackageReference,
    *,
    undo_stack: list[ArchitectMutation] | None = None,
) -> PackageReference:
    command, _, rest = line.partition(" ")
    command = command.strip().lower()
    rest = rest.strip()
    if command in {"packages", "projects"}:
        _print_project_list(project_list_report(with_status=False))
        return current_ref
    if command == "use":
        if not rest:
            raise SemanticLayerError("INVALID_CONFIG", "Usage: use <package-id|path>")
        if Path(rest).exists():
            current_ref = resolve_package_reference(path=rest)
        else:
            current_ref = resolve_package_reference(package_id=rest)
        print(f"Using {_ref_label(current_ref)}")
        return current_ref
    if command in {"debug", "status"}:
        _print_project_status(project_status_report(current_ref, checks="parse"))
        return current_ref
    if command in {"validate", "test"}:
        mode = rest.lower() or "parse"
        if mode not in PROJECT_CHECK_MODES:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Usage: validate [parse|runtime|examples|tests|full]",
            )
        if mode in {"runtime", "examples", "tests", "full"}:
            warehouse = _authoring_warehouse(current_ref)
            print(f"Operational check: {mode} may query or refresh the {warehouse} warehouse.")
            try:
                confirmed = _author_confirm("Continue with operational validation?", default=False)
            except _AuthoringCancelled:
                confirmed = False
            if not confirmed:
                print("Validation cancelled. Run `validate` for a safe parse-only check.")
                return current_ref
        else:
            print("Safe check: parsing package YAML without warehouse queries.")
        _print_project_validation(project_validation_report(current_ref, mode=mode))
        return current_ref
    if command == "ls":
        parts = rest.split(maxsplit=1)
        kind = parts[0] if parts and parts[0] in CATALOG_KINDS else "all"
        search = parts[1] if kind != "all" and len(parts) > 1 else (rest if kind == "all" else "")
        _print_objects(
            list_objects_report(current_ref, resource_type=kind, search=search, limit=30)
        )
        return current_ref
    if command in {"ask", "plan"}:
        _print_ask_report(ask_report(current_ref, question=rest))
        return current_ref
    if command == "run":
        _print_ask_report(ask_report(current_ref, question=rest, execute=True))
        return current_ref
    if command in {"author", "create", "manage"}:
        mutation = _run_authoring_flow(current_ref, requested_kind=rest)
        if mutation is not None and undo_stack is not None:
            undo_stack.append(mutation)
        return current_ref
    if command == "undo":
        if not undo_stack:
            print("Nothing to undo in this REPL session.")
            return current_ref
        current_project = Path(package_root_for_source(current_ref.source_path)).resolve()
        matching_index = next(
            (
                index
                for index in range(len(undo_stack) - 1, -1, -1)
                if Path(undo_stack[index].project_path).resolve() == current_project
            ),
            None,
        )
        if matching_index is None:
            print("Nothing to undo for the active package.")
            return current_ref
        mutation = undo_stack[matching_index]
        report = mutation.undo()
        if report.get("status") == "undo_conflict":
            print("[error] Undo was not applied because an authored file changed afterward.")
            for relative_path in list(report.get("conflicting_files", []) or []):
                print(f"  conflict {mutation.project_path / relative_path}")
            for message in _authoring_error_messages(report)[:5]:
                print(f"  - {message}")
            return current_ref
        undo_stack.pop(matching_index)
        changed = ", ".join(report.get("changed_files", []) or []) or "authored files"
        print(f"Undid the last authoring change in {mutation.project_path}: {changed}")
        if not report.get("ok"):
            print("[warning] Files were restored, but the package still has parse errors:")
            for message in _authoring_error_messages(report)[:5]:
                print(f"  - {message}")
        return current_ref
    raise SemanticLayerError(
        "INVALID_CONFIG",
        f"Unknown interactive command '{command}'. Type help for commands.",
    )


def _print_repl_help() -> None:
    visual, color = _repl_capabilities()
    if visual:

        def accent(text: str) -> str:
            return _repl_color(text, "36", enabled=color)

        print()
        print(_repl_color("Commands", "1", enabled=color))
        rows = (
            ("packages", "List registered packages"),
            ("use <package|path>", "Switch package"),
            ("debug", "Show package status"),
            ("validate [mode]", "Parse safely; runtime/full ask first"),
            ("ls [kind] [search]", "List catalog objects"),
            ("author [kind]", "Create or update a semantic abstraction"),
            ("undo", "Undo the last authoring change this session"),
            ("ask <question>", "Plan a query"),
            ("run <question>", "Plan and execute a query"),
            ("exit", "Quit"),
        )
        for command, description in rows:
            print(f"  {accent(command.ljust(22))} {description}")
        print()
        return

    print("Commands:")
    print("  packages                 List registered packages")
    print("  use <package|path>       Switch package")
    print("  debug                    Show package status")
    print("  validate [mode]          Parse safely; runtime/full ask first")
    print("  ls [kind] [search]       List catalog objects")
    print("  author [kind]            Create or update a semantic abstraction")
    print("  undo                     Undo the last authoring change this session")
    print("  ask <question>           Plan a query")
    print("  run <question>           Plan and execute a query")
    print("  exit                     Quit")


class _AuthoringCancelled(Exception):
    """Return from a nested authoring wizard without ending the REPL."""


_AUTHORING_KINDS = ("model", "dimension", "time", "measure", "metric", "segment")
_AUTHORING_ALIASES = {
    "entity": "model",
    "model/entity": "model",
    "time_dimension": "time",
    "times": "time",
    "dimensions": "dimension",
    "measures": "measure",
    "metrics": "metric",
    "segments": "segment",
}


def _run_authoring_flow(
    current_ref: PackageReference,
    *,
    requested_kind: str = "",
) -> ArchitectMutation | None:
    try:
        if not sys.stdin.isatty():
            raise SemanticLayerError(
                "INVALID_CONFIG",
                (
                    "Guided authoring needs an interactive terminal. Re-run `semantic-rails repl "
                    f"--path {_quote(current_ref.source_path)}` in a terminal, then type `author`."
                ),
            )
        source = Path(current_ref.source_path).expanduser().resolve()
        if not source.is_dir() or not (source / "package.yml").is_file():
            raise SemanticLayerError(
                "INVALID_CONFIG",
                (
                    "Guided authoring supports split-layout package directories. "
                    "Create one with `semantic-rails init <name>` or manage this legacy "
                    "single-file package directly."
                ),
                details={"source_path": str(source)},
            )
        project = ArchitectProject(source, workspace_root=source.parent)
        initial = project_validation_report(current_ref, mode="parse")
        if not initial.get("ok"):
            print("Cannot author safely while the package has parse errors.")
            for message in _authoring_error_messages(initial)[:5]:
                print(f"  [error] {message}")
            print("Fix those errors, then run `author` again.")
            return None

        inventory = project.inventory()
        kind = _resolve_authoring_kind(requested_kind, inventory)
        print()
        print(f"Authoring {_ref_label(current_ref)} - {kind}")
        print("Type `cancel` at any prompt to return without writing files.")

        before_warnings = set(_authoring_warning_messages(initial))
        dispatch = {
            "model": _author_model,
            "dimension": _author_dimension,
            "time": _author_time,
            "measure": _author_measure,
            "metric": _author_metric,
            "segment": _author_segment,
        }
        mutation = dispatch[kind](project, inventory, current_ref, before_warnings)
        return mutation
    except _AuthoringCancelled:
        print("Authoring cancelled; no files changed.")
        return None
    except (EOFError, KeyboardInterrupt):
        print("\nAuthoring cancelled; no files changed.")
        return None


def _resolve_authoring_kind(requested: str, inventory: dict[str, Any]) -> str:
    raw = str(requested or "").strip().lower().replace(" ", "_")
    raw = _AUTHORING_ALIASES.get(raw, raw)
    if raw:
        if raw not in _AUTHORING_KINDS:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Usage: author [model|dimension|time|measure|metric|segment]",
            )
        return raw

    recommended = "model"
    if _inventory_items(inventory, "model"):
        recommended = "measure" if not _inventory_items(inventory, "measure") else "metric"
    options = [
        ("model", "Model/entity - connect a table and define its business grain"),
        ("dimension", "Dimension - something people group or filter by"),
        ("time", "Time - when an event or state occurred"),
        ("measure", "Measure - a primitive count, sum, or aggregatable fact"),
        ("metric", "Metric - a stable governed KPI built from measures or metrics"),
        ("segment", "Segment - a reusable entity cohort, such as high-value customers"),
    ]
    return _author_choice("What do you want to create or update?", options, default=recommended)


def _author_model(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    key, label, existing = _author_identity(project, inventory, "model", "orders")
    spec = dict(existing.get("spec", {}) or {}) if existing else {}
    existing_entity = _model_entity_defaults(project, key)
    relation = _author_prompt(
        "Warehouse table or relation (for example raw_orders)",
        str(spec.get("relation", key)),
    )
    entity_key = _author_slug_prompt(
        "Business entity at one row of this model",
        str(existing_entity.get("key", key.rstrip("s") or key)),
    )
    entity_conflict = next(
        (
            row
            for row in _inventory_items(inventory, "entity")
            if str(row.get("key", "")) == entity_key
            and str((row.get("spec", {}) or {}).get("model", "")) != key
        ),
        None,
    )
    if entity_conflict:
        owner = str((entity_conflict.get("spec", {}) or {}).get("model", ""))
        raise SemanticLayerError(
            "INVALID_CONFIG",
            (
                f"Entity `{entity_key}` already belongs to model `{owner}`. "
                "Choose a different entity key or manage its existing model."
            ),
        )
    primary_default = ", ".join(existing_entity.get("primary_key", []) or []) or f"{entity_key}_id"
    primary_key = [
        _slug(part, fallback=f"{entity_key}_id")
        for part in _author_prompt("Primary key column(s), comma separated", primary_default).split(
            ","
        )
        if part.strip()
    ]
    description = _author_prompt(
        "Description",
        str(spec.get("description", f"One row per {entity_key.replace('_', ' ')}.")),
    )
    preview = {
        "model": {
            "id": key,
            "label": label,
            "relation": relation,
            "entities": {entity_key: {}},
            "description": description,
        },
        "graph": {
            "entities": {
                entity_key: {
                    "label": _title(entity_key),
                    "key": primary_key,
                    "model": key,
                    "allowed_as_root": True,
                }
            }
        },
    }
    target = str(existing.get("relative_path", "")) if existing else f"models/core/{key}.yml"
    return _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="model",
        key=key,
        label=label,
        existing=existing,
        target=target,
        preview=preview,
        apply=lambda: project.upsert_model(
            model_id=key,
            entity_key=entity_key,
            relation=relation,
            primary_key=primary_key,
            description=description,
            label=label,
        ),
        next_action="Add a dimension with `author dimension`.",
    )


def _author_dimension(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    model = _select_model(inventory)
    key, label, existing = _author_identity(
        project, inventory, "dimension", "status", parent=str(model.get("key", ""))
    )
    current = dict(existing.get("spec", {}) or {}) if existing else {}
    kind = _author_choice(
        "How should values behave?",
        [
            ("categorical", "Categorical - labels such as placed, shipped, cancelled"),
            ("boolean", "Boolean - true/false"),
            ("integer", "Integer - whole numbers that can be grouped or filtered"),
            ("continuous", "Continuous - numeric values used as dimensions"),
        ],
        default=str(current.get("kind", "categorical")),
    )
    column = _author_slug_prompt("Backing column", str(current.get("expr", key)))
    description = _author_prompt(
        "Description", str(current.get("description", f"{label} used for grouping and filtering."))
    )
    dimension = {**current, "label": label, "description": description, "kind": kind}
    if column != key:
        dimension["expr"] = column
    else:
        dimension.pop("expr", None)
    target = str(model.get("relative_path", ""))
    return _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="dimension",
        key=key,
        label=label,
        existing=existing,
        target=target,
        preview={"dimensions": {key: dimension}},
        apply=lambda: _upsert_nested_model(project, model, dimensions={key: dimension}),
        next_action=f"Try `ls dimension {key}` after validation.",
    )


def _author_time(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    model = _select_model(inventory)
    model_key = str(model.get("key", ""))
    key, label, existing = _author_identity(
        project, inventory, "time", "occurred_at", parent=model_key
    )
    current = dict(existing.get("spec", {}) or {}) if existing else {}
    column = _author_slug_prompt("Date or timestamp column", str(current.get("column", key)))
    kind = _author_choice(
        "Column type",
        [("timestamp", "Timestamp - date and time"), ("date", "Date - calendar date only")],
        default=str(current.get("kind", "timestamp")),
    )
    clock_class = _author_choice(
        "What does this clock mean?",
        [
            ("event_time", "Event time - when an event happened"),
            ("state_time", "State time - when a snapshot was observed"),
            ("as_of_time", "As-of time - effective point for temporal joins"),
            ("calendar_time", "Calendar time - a date-spine or calendar model"),
        ],
        default=str(current.get("class", "event_time")),
    )
    model_times = dict((model.get("spec", {}) or {}).get("times", {}) or {})
    should_default = bool(current.get("default", not model_times))
    make_default = _author_confirm(
        "Use this as the model's default query clock?", default=should_default
    )
    time_spec: dict[str, Any] = {
        **current,
        "label": label,
        "column": column,
        "kind": kind,
        "class": clock_class,
        "default": make_default,
        "default_query_axis": make_default,
    }
    updates: dict[str, Any] = {key: time_spec}
    if make_default:
        for other_key, other_spec in model_times.items():
            if other_key == key:
                continue
            updates[str(other_key)] = {
                **dict(other_spec or {}),
                "default": False,
                "default_query_axis": False,
            }
    return _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="time",
        key=key,
        label=label,
        existing=existing,
        target=str(model.get("relative_path", "")),
        preview={"times": updates},
        apply=lambda: _upsert_nested_model(project, model, times=updates),
        next_action="Run `validate runtime` when you are ready to test warehouse data.",
    )


def _author_measure(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    model = _select_model(inventory)
    key, label, existing = _author_identity(
        project, inventory, "measure", "revenue", parent=str(model.get("key", ""))
    )
    current = dict(existing.get("spec", {}) or {}) if existing else {}
    measure_kind = _author_choice(
        "What primitive fact is this?",
        [
            ("aggregate", "Aggregate - sum, average, minimum, or maximum"),
            ("entity_count", "Entity count - distinct count of business keys"),
        ],
        default=str(current.get("kind", "aggregate")),
    )
    description = _author_prompt(
        "Description", str(current.get("description", f"Governed {label.lower()} primitive."))
    )
    if measure_kind == "entity_count":
        entity_defaults = _model_entity_defaults(project, str(model.get("key", "")))
        entity_key = _author_slug_prompt(
            "Entity key column",
            str(
                current.get("entity_key", next(iter(entity_defaults.get("primary_key", [])), "id"))
            ),
        )
        measure: dict[str, Any] = {
            **current,
            "label": label,
            "description": description,
            "kind": "entity_count",
            "entity_key": entity_key,
            "accumulation": {"kind": "event"},
            "value_type": "count",
            "meta": {
                "owner_team": "analytics",
                "review_priority": "medium",
                "change_risk": "medium",
                **dict(current.get("meta", {}) or {}),
            },
        }
        if current.get("kind") != "entity_count":
            for stale_key in ("expr", "default_agg", "currency", "rollup"):
                measure.pop(stale_key, None)
    else:
        expression = _author_prompt(
            "Column or scalar expression (for example amount_cents / 100.0)",
            str(current.get("expr", key)),
        )
        aggregation = _author_choice(
            "Default aggregation",
            [(value, value.replace("_", " ").title()) for value in ("sum", "avg", "min", "max")],
            default=str(current.get("default_agg", "sum")),
        )
        value_type = _author_choice(
            "Result type",
            [
                ("number", "Number"),
                ("currency", "Currency"),
                ("percent", "Percent"),
                ("count", "Count"),
            ],
            default=str(current.get("value_type", "number")),
        )
        measure = {
            **current,
            "label": label,
            "description": description,
            "kind": "aggregate",
            "expr": expression,
            "default_agg": aggregation,
            "accumulation": {"kind": "flow"},
            "value_type": value_type,
            "meta": {
                "owner_team": "analytics",
                "review_priority": "medium",
                "change_risk": "medium",
                **dict(current.get("meta", {}) or {}),
            },
        }
        if current.get("kind") != "aggregate":
            measure.pop("entity_key", None)
        if value_type == "currency":
            measure["currency"] = _author_prompt(
                "Currency code", str(current.get("currency", "USD"))
            ).upper()
    return _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="measure",
        key=key,
        label=label,
        existing=existing,
        target=str(model.get("relative_path", "")),
        preview={"measures": {key: measure}},
        apply=lambda: _upsert_nested_model(project, model, measures={key: measure}),
        next_action="Publish this primitive as a stable KPI with `author metric`.",
    )


def _author_metric(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    measures = _inventory_items(inventory, "measure")
    metrics = _inventory_items(inventory, "metric")
    if not measures:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Add a primitive measure first with `author measure`, then create a governed metric.",
        )
    published_measures = {str((row.get("spec", {}) or {}).get("measure", "")) for row in metrics}
    recommended_measure = next(
        (row for row in measures if str(row.get("key", "")) not in published_measures),
        measures[0],
    )
    recommended_key = str(recommended_measure.get("key", "metric"))
    key, label, existing = _author_identity(project, inventory, "metric", recommended_key)
    current = dict(existing.get("spec", {}) or {}) if existing else {}
    metric_kind = _author_choice(
        "Metric recipe",
        [
            ("aggregate", "Aggregate - publish one measure as a stable KPI"),
            ("ratio", "Ratio - divide one measure/metric by another"),
        ],
        default=str(current.get("kind", "aggregate"))
        if current.get("kind") in {"aggregate", "ratio"}
        else "aggregate",
    )
    description = _author_prompt(
        "Business definition",
        str(current.get("description", f"Governed definition of {label.lower()}.")),
    )
    matching_measure = next(
        (row for row in measures if str(row.get("key", "")) == key),
        None,
    )
    value_default = str(
        current.get("value_type")
        or ((matching_measure or {}).get("spec", {}) or {}).get("value_type")
        or "number"
    )
    value_options = [
        ("number", "Number"),
        ("currency", "Currency"),
        ("percent", "Percent"),
        ("count", "Count"),
    ]
    if metric_kind == "ratio":
        value_options = [
            ("percent", "Percent or share"),
            ("ratio", "Dimensionless ratio"),
            ("currency", "Currency per unit"),
        ]
        if value_default not in {value for value, _ in value_options}:
            value_default = "percent"
    value_type = _author_choice(
        "Result type",
        value_options,
        default=value_default
        if value_default in {value for value, _ in value_options}
        else value_options[0][0],
    )
    namespace = _authoring_namespace(project)
    metric_id = str(current.get("as") or current.get("id") or "")
    if not metric_id:
        metric_id = f"metric.{key}" if "." in key else f"metric.{namespace}.{key}"
    spec: dict[str, Any] = {
        **current,
        "as": metric_id,
        "label": label,
        "description": description,
        "kind": metric_kind,
        "value_type": value_type,
        "meta": {
            "owner_team": "analytics",
            "review_priority": "medium",
            "change_risk": "medium",
            **dict(current.get("meta", {}) or {}),
        },
    }
    if metric_kind == "aggregate":
        for stale_key in ("numerator", "denominator", "null_behavior", "expression"):
            spec.pop(stale_key, None)
        source_default = str(current.get("measure", ""))
        if not source_default and matching_measure is not None:
            source_default = key
        selected = _select_inventory_item(
            "Measure to publish",
            measures,
            default_key=source_default or None,
        )
        spec["measure"] = str(selected.get("id") or selected.get("key", ""))
        example = f"What is {label.lower()} by month?"
    else:
        spec.pop("measure", None)
        spec.pop("aggregation", None)
        spec.pop("expression", None)
        operands: list[dict[str, Any]] = []
        seen_operand_ids: set[str] = set()
        for row in [*measures, *metrics]:
            operand_id = str(row.get("id") or row.get("key", ""))
            if (
                not operand_id
                or operand_id in seen_operand_ids
                or (row.get("kind") == "metric" and str(row.get("key", "")) == key)
            ):
                continue
            operands.append(row)
            seen_operand_ids.add(operand_id)
        numerator_default = str(current.get("numerator", ""))
        if not numerator_default and any(str(row.get("key", "")) == key for row in operands):
            numerator_default = key
        numerator = _select_inventory_item(
            "Numerator", operands, default_key=numerator_default or None
        )
        denominator_options = [
            row
            for row in operands
            if str(row.get("id") or row.get("key", ""))
            != str(numerator.get("id") or numerator.get("key", ""))
        ]
        if not denominator_options:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "A ratio needs two distinct measures or metrics. Add another primitive first.",
            )
        denominator_default = str(current.get("denominator", ""))
        if not any(
            denominator_default in {str(row.get("key", "")), str(row.get("id", ""))}
            for row in denominator_options
        ):
            denominator_default = str(
                denominator_options[0].get("id") or denominator_options[0].get("key", "")
            )
        denominator = _select_inventory_item(
            "Denominator", denominator_options, default_key=denominator_default
        )
        spec.update(
            {
                "numerator": str(numerator.get("id") or numerator.get("key", "")),
                "denominator": str(denominator.get("id") or denominator.get("key", "")),
                "null_behavior": "null_if_zero",
            }
        )
        example = f"How does {label.lower()} trend by month?"
    if value_type == "currency":
        spec["currency"] = _author_prompt(
            "Currency code", str(current.get("currency", "USD"))
        ).upper()
    temporal = _default_temporal_reference(inventory)
    if temporal:
        spec["temporal_role"] = temporal
    spec["examples"] = [example]
    target = (
        str(existing.get("relative_path", ""))
        if existing
        else f"metrics/core/{_slug(key, fallback='metric')}.yml"
    )
    return _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="metric",
        key=key,
        label=label,
        existing=existing,
        target=target,
        preview={"metrics": {key: spec}},
        apply=lambda: project.upsert_metric(metric_key=key, spec=spec, group="core", replace=True),
        next_action=f"Try `ask {example}`.",
    )


def _author_segment(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    entities = _inventory_items(inventory, "entity")
    dimensions = _inventory_items(inventory, "dimension")
    metrics = _inventory_items(inventory, "metric")
    if not entities or not dimensions or not metrics:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Segments need an entity, a dimension, and a metric. Add those abstractions first.",
        )
    key, label, existing = _author_identity(project, inventory, "segment", "active_customers")
    current = dict(existing.get("spec", {}) or {}) if existing else {}
    entity = _select_inventory_item("Entity whose members this segment contains", entities)
    entity_model = str((entity.get("spec", {}) or {}).get("model", ""))
    entity_dimensions = [row for row in dimensions if str(row.get("parent", "")) == entity_model]
    if not entity_dimensions:
        entity_dimensions = dimensions
    current_preview = list(current.get("preview_dimensions", []) or [])
    dimension = _select_inventory_item(
        "Membership dimension",
        entity_dimensions,
        default_key=str(current_preview[0]) if current_preview else "",
    )
    model_clock_ids = {
        str(row.get("id", ""))
        for row in _inventory_items(inventory, "time")
        if str(row.get("parent", "")) == entity_model
    }
    compatible_metrics = [
        row
        for row in metrics
        if not model_clock_ids
        or not str((row.get("spec", {}) or {}).get("temporal_role", ""))
        or str((row.get("spec", {}) or {}).get("temporal_role", "")) in model_clock_ids
    ]
    if not compatible_metrics:
        compatible_metrics = metrics
    basis = _select_inventory_item(
        "Metric used when previewing segment size",
        compatible_metrics,
        default_key=str(current.get("basis_metric", "")),
    )
    dimension_kind = str((dimension.get("spec", {}) or {}).get("kind", "categorical"))
    comparison_options = [("=", "Equals"), ("!=", "Does not equal")]
    if dimension_kind in {
        "integer",
        "continuous",
        "number",
        "percent",
        "currency",
        "date",
        "timestamp",
    }:
        comparison_options.extend(
            [(">=", "At least"), (">", "Greater than"), ("<=", "At most"), ("<", "Less than")]
        )
    current_where = list((current.get("membership", {}) or {}).get("where", []) or [])
    current_filter = dict(current_where[0] or {}) if current_where else {}
    current_op = str(current_filter.get("op", "="))
    op = _author_choice(
        "Membership comparison",
        comparison_options,
        default=current_op if current_op in {value for value, _ in comparison_options} else "=",
    )
    domain = list((dimension.get("spec", {}) or {}).get("domain", []) or [])
    domain_default: Any = "true" if dimension_kind == "boolean" else ""
    if domain:
        first_domain = domain[0]
        domain_default = (
            first_domain.get("value", "") if isinstance(first_domain, dict) else first_domain
        )
    value_default = current_filter.get("value", domain_default)
    value = _author_scalar(
        _author_prompt(
            "Comparison value (use a value from the authored domain when available)",
            str(value_default),
        )
    )
    description = _author_prompt(
        "Description",
        str(
            current.get(
                "description",
                f"{label} where {dimension.get('label') or dimension.get('key')} {op} {value}.",
            )
        ),
    )
    namespace = _authoring_namespace(project)
    entity_ref = str(entity.get("id") or entity.get("key", ""))
    dimension_ref = str(dimension.get("id") or dimension.get("key", ""))
    basis_ref = str(basis.get("id") or basis.get("key", ""))
    segment_id = str(current.get("id") or "")
    if not segment_id:
        segment_id = f"segment.{key}" if "." in key else f"segment.{namespace}.{key}"
    spec = {
        **current,
        "id": segment_id,
        "label": label,
        "description": description,
        "entity": entity_ref,
        "basis_metric": basis_ref,
        "preview_dimensions": [dimension_ref],
        "membership": {"where": [{"field": dimension_ref, "op": op, "value": value}]},
    }
    target = str(existing.get("relative_path", "")) if existing else "segments/core.yml"
    return _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="segment",
        key=key,
        label=label,
        existing=existing,
        target=target,
        preview={"segments": {key: spec}},
        apply=lambda: project.upsert_segment(segment_key=key, spec=spec, file_name="core.yml"),
        next_action=f"Inspect it with `ls segment {key}`.",
    )


def _upsert_nested_model(
    project: ArchitectProject, model: dict[str, Any], **updates: Any
) -> ArchitectMutation:
    model_key = str(model.get("key", ""))
    spec = dict(model.get("spec", {}) or {})
    entity_defaults = _model_entity_defaults(project, model_key)
    return project.upsert_model(
        model_id=model_key,
        entity_key=str(entity_defaults.get("key", model_key.rstrip("s") or model_key)),
        relation=str(spec.get("relation", model_key)),
        primary_key=list(entity_defaults.get("primary_key", []) or [f"{model_key.rstrip('s')}_id"]),
        description=str(spec.get("description", "")),
        **updates,
    )


def _apply_authoring_change(
    project: ArchitectProject,
    ref: PackageReference,
    before_warnings: set[str],
    *,
    kind: str,
    key: str,
    label: str,
    existing: dict[str, Any] | None,
    target: str,
    preview: dict[str, Any],
    apply: Any,
    next_action: str,
) -> ArchitectMutation:
    operation = "update" if existing else "create"
    print()
    print(f"Preview - {operation} {kind} `{key}`")
    print(f"  label   {label}")
    print(f"  file    {target}")
    rendered = yaml.safe_dump(preview, sort_keys=False, allow_unicode=False).rstrip()
    for line in rendered.splitlines():
        print(f"  {line}")
    similar = project.find_similar(kind=kind, key=key, label=label)
    if similar:
        print("\n[warning] This sounds similar to existing definitions:")
        for row in similar[:3]:
            reason = str(row.get("reason", "similar wording"))
            print(
                f"  - {row.get('kind', kind)} {row.get('id') or row.get('key')} "
                f"- {row.get('label', '')} ({reason})"
            )
        decision = _author_choice(
            "How should we proceed?",
            [
                ("restart", "Cancel and restart with different wording"),
                ("continue", "Create it as a deliberately distinct definition"),
                ("cancel", "Cancel without writing"),
            ],
            default="restart",
        )
        if decision != "continue":
            raise _AuthoringCancelled
    if not _author_confirm(f"{operation.title()} this {kind}?", default=False):
        raise _AuthoringCancelled

    mutation = apply()
    report = mutation.report
    if not report.get("ok"):
        status = str(report.get("status", "failed"))
        print(f"[error] Authoring {status}; original files were restored.")
        for message in _authoring_error_messages(report)[:5]:
            print(f"  - {message}")
        raise _AuthoringCancelled

    after_warnings = set(_authoring_warning_messages(report.get("parse", {})))
    new_warnings = sorted(after_warnings - before_warnings)
    unchanged = len(after_warnings & before_warnings)
    completed_operation = f"{operation}d"
    print(f"[ok] {kind.title()} {completed_operation} and parse-validated.")
    print(f"  changed {', '.join(report.get('changed_files', []) or [target])}")
    if new_warnings:
        print(f"[warning] {len(new_warnings)} new authoring warning(s):")
        for warning in new_warnings[:5]:
            print(f"  - {warning}")
    if unchanged:
        print(
            f"  {unchanged} existing warning(s) unchanged; run `validate` to inspect package health."
        )
    print(f"  next    {next_action}")
    print("  undo    Type `undo` to restore the previous files in this REPL session.")
    return mutation


def _author_identity(
    project: ArchitectProject,
    inventory: dict[str, Any],
    kind: str,
    default_key: str,
    *,
    parent: str = "",
) -> tuple[str, str, dict[str, Any] | None]:
    raw = _author_prompt(f"{kind.title()} key", default_key)
    key = (
        ".".join(_slug(part, fallback="item") for part in raw.split("."))
        if kind in {"metric", "segment"} and "." in raw
        else _slug(raw, fallback=default_key)
    )
    if key != raw:
        print(f"  normalized `{raw}` -> `{key}` (YAML-safe key)")
    candidates = _inventory_items(inventory, kind)
    if kind == "measure" and parent:
        other_parent = next(
            (
                row
                for row in candidates
                if str(row.get("key", "")) == key and str(row.get("parent", "")) != parent
            ),
            None,
        )
        if other_parent:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                (
                    f"Measure `{key}` already exists on model `{other_parent.get('parent')}`. "
                    "Measure IDs are package-wide; choose a different key or manage that model."
                ),
            )
    existing = next(
        (
            row
            for row in candidates
            if str(row.get("key", "")) == key
            and (not parent or str(row.get("parent", "")) == parent)
        ),
        None,
    )
    if existing:
        print(
            f"[warning] `{key}` already exists in {existing.get('relative_path', 'the package')} "
            f"as {existing.get('label') or key}."
        )
        if not _author_confirm(f"Manage and update this existing {kind}?", default=False):
            raise _AuthoringCancelled
    default_label = str(existing.get("label", "")) if existing else _title(key)
    label = _author_prompt("Business label", default_label)
    return key, label, existing


def _select_model(inventory: dict[str, Any]) -> dict[str, Any]:
    models = _inventory_items(inventory, "model")
    if not models:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Create a model/entity first with `author model`.",
        )
    return _select_inventory_item("Model to extend", models)


def _select_inventory_item(
    label: str,
    rows: list[dict[str, Any]],
    *,
    default_key: str | None = "",
) -> dict[str, Any]:
    if not rows:
        raise SemanticLayerError("INVALID_CONFIG", f"No choices are available for {label.lower()}")
    visible = list(rows)
    if len(visible) > 12:
        while True:
            search = _author_prompt(
                f"Filter {label.lower()} ({len(visible)} choices; type words, or Enter for a short list)",
                "",
            ).lower()
            if not search:
                preferred = next(
                    (
                        row
                        for row in visible
                        if default_key
                        and default_key in {str(row.get("key", "")), str(row.get("id", ""))}
                    ),
                    None,
                )
                visible = ([preferred] if preferred else []) + [
                    row for row in visible if row is not preferred
                ][: 12 - bool(preferred)]
                print("Showing a short list. Type search words at the filter prompt to narrow it.")
                break
            matched = [
                row
                for row in visible
                if search
                in " ".join(
                    str(row.get(field, ""))
                    for field in ("kind", "key", "id", "label", "description", "parent")
                ).lower()
            ]
            if matched:
                visible = matched[:12]
                print(f"Found {len(matched)} matching choice(s).")
                break
            print("No matching objects. Try a key, label, ID, or model name.")
    default_index = (
        0
        if default_key == ""
        else next(
            (
                index
                for index, row in enumerate(visible)
                if default_key and default_key in {str(row.get("key", "")), str(row.get("id", ""))}
            ),
            None,
        )
    )
    options: list[tuple[str, str]] = []
    lookup: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(visible, start=1):
        value = str(index)
        key = str(row.get("key") or row.get("id") or value)
        kind = str(row.get("kind", "object"))
        description = f"[{kind}] {row.get('label') or key}"
        if row.get("parent"):
            description += f" - {row['parent']}"
        options.append((value, f"{key} - {description}"))
        lookup[value] = row
    selected = _author_choice(
        label,
        options,
        default=str(default_index + 1) if default_index is not None else "",
    )
    return lookup[selected]


def _author_choice(label: str, options: list[tuple[str, str]], *, default: str) -> str:
    print(f"{label}")
    for index, (value, description) in enumerate(options, start=1):
        recommended = " (recommended)" if value == default else ""
        print(f"  {index}. {description}{recommended}")
    aliases = {str(index): value for index, (value, _) in enumerate(options, start=1)}
    values = {value for value, _ in options}
    while True:
        raw = _author_prompt("Choose", default).lower()
        selected = aliases.get(raw, raw)
        if selected == "cancel":
            raise _AuthoringCancelled
        if selected in values:
            return selected
        print("Choose a number or one of: " + ", ".join(value for value, _ in options))


def _author_prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise _AuthoringCancelled from exc
    if value.lower() in {"cancel", ":q", "quit"}:
        raise _AuthoringCancelled
    return value or default


def _author_confirm(label: str, *, default: bool) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    try:
        value = input(f"{label}{suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt) as exc:
        raise _AuthoringCancelled from exc
    if value in {"cancel", ":q", "quit"}:
        raise _AuthoringCancelled
    if not value:
        return default
    return value in {"y", "yes", "true", "1"}


def _author_slug_prompt(label: str, default: str) -> str:
    raw = _author_prompt(label, default)
    value = _slug(raw, fallback=default)
    if value != raw:
        print(f"  normalized `{raw}` -> `{value}`")
    return value


def _inventory_items(inventory: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    key = {"entity": "entities", "time": "times"}.get(kind, f"{kind}s")
    return [dict(row) for row in list(inventory.get(key, []) or [])]


def _model_entity_defaults(project: ArchitectProject, model_id: str) -> dict[str, Any]:
    graph_path = Path(project.project_path) / "graph.yml"
    raw = (
        dict(yaml.safe_load(graph_path.read_text(encoding="utf-8")) or {})
        if graph_path.is_file()
        else {}
    )
    entities = dict((raw.get("graph", {}) or {}).get("entities", {}) or {})
    for key, spec_raw in entities.items():
        spec = dict(spec_raw or {})
        if str(spec.get("model", "")) == model_id:
            primary = spec.get("key", [])
            keys = [str(item) for item in primary] if isinstance(primary, list) else [str(primary)]
            return {"key": str(key), "primary_key": [item for item in keys if item]}
    return {"key": model_id.rstrip("s") or model_id, "primary_key": []}


def _authoring_namespace(project: ArchitectProject) -> str:
    package_path = Path(project.project_path) / "package.yml"
    raw = dict(yaml.safe_load(package_path.read_text(encoding="utf-8")) or {})
    package = dict(raw.get("package", {}) or {})
    return _slug(
        str(package.get("namespace") or package.get("id") or Path(project.project_path).name)
    )


def _authoring_warehouse(ref: PackageReference) -> str:
    source = Path(ref.source_path)
    package_path = source / "package.yml" if source.is_dir() else source
    try:
        raw = dict(yaml.safe_load(package_path.read_text(encoding="utf-8")) or {})
    except (OSError, yaml.YAMLError):
        return "configured"
    return str((raw.get("package", {}) or {}).get("warehouse", "configured") or "configured")


def _default_temporal_reference(inventory: dict[str, Any]) -> str:
    times = _inventory_items(inventory, "time")
    if not times:
        return ""
    selected = next(
        (row for row in times if bool((row.get("spec", {}) or {}).get("default"))),
        times[0],
    )
    return str(selected.get("id") or selected.get("key") or "")


def _author_scalar(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def _authoring_warning_messages(report: dict[str, Any]) -> list[str]:
    warnings = list(report.get("warnings", []) or [])
    parse = report.get("parse", {})
    if isinstance(parse, dict):
        warnings.extend(list(parse.get("warnings", []) or []))
    messages: list[str] = []
    for warning in warnings:
        message = str(warning.get("message", "")) if isinstance(warning, dict) else str(warning)
        if message and message not in messages:
            messages.append(message)
    return messages


def _authoring_error_messages(report: dict[str, Any]) -> list[str]:
    errors = list(report.get("errors", []) or [])
    parse = report.get("parse", {})
    if isinstance(parse, dict):
        errors.extend(list(parse.get("errors", []) or []))
    messages: list[str] = []
    for error in errors:
        message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
        if message and message not in messages:
            messages.append(message)
    return messages


def project_status_report(ref: PackageReference, *, checks: str = "parse") -> dict[str, Any]:
    root = Path(package_root_for_source(ref.source_path))
    files = _project_files(root)
    validation = project_validation_report(ref, mode=checks)
    summary = dict(validation.get("summary", {}) or {})
    parse = validation.get("checks", {}).get("parse", {}) or validation.get("parse", {})
    return {
        "ok": bool(validation.get("ok")),
        "package": dict(validation.get("package", {}) or _ref_payload(ref)),
        "source_path": ref.source_path,
        "project_root": str(root),
        "layout": "directory" if Path(ref.source_path).is_dir() else "single_file",
        "files": files,
        "summary": summary,
        "parse": parse,
        "checks": dict(validation.get("checks", {}) or {}),
        "next_actions": _status_next_actions(ref, validation),
    }


def project_validation_report(
    ref: PackageReference,
    *,
    mode: str = "full",
    compare_path: str = "",
    base_ref: str = "",
) -> dict[str, Any]:
    selected = str(mode or "full").strip().lower()
    if selected not in PROJECT_CHECK_MODES:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Unsupported validation mode '{mode}'",
            details={"supported_modes": list(PROJECT_CHECK_MODES)},
        )
    if selected == "parse":
        report, _ = parse_config_report(ref, progress=lambda _: None)
        return {
            "ok": bool(report.get("ok")),
            "package": dict(report.get("package", {}) or _ref_payload(ref)),
            "summary": {"parse": _parse_summary(report)},
            "checks": {"parse": _parse_summary(report)},
            "parse": report,
            "errors": list(report.get("errors", []) or []),
            "warnings": list(report.get("warnings", []) or []),
        }
    if selected == "runtime":
        report = validate_config_report(ref, progress=lambda _: None)
        return {
            "ok": bool(report.get("ok")),
            "package": dict(report.get("package", {}) or _ref_payload(ref)),
            "summary": {"runtime": _validate_summary(report)},
            "checks": {"runtime": _validate_summary(report)},
            "runtime": report,
            "errors": list(report.get("errors", []) or []),
        }
    if selected == "examples":
        report = run_examples_report(ref)
        return {
            "ok": bool(report.get("ok")),
            "package": dict(report.get("package", {}) or _ref_payload(ref)),
            "summary": {"examples": dict(report.get("summary", {}) or {})},
            "checks": {"examples": _ok_summary(report, "examples")},
            "examples": report,
            "errors": list(report.get("errors", []) or []),
        }
    if selected == "tests":
        report = run_package_tests_report(ref)
        return {
            "ok": bool(report.get("ok")),
            "package": dict(report.get("package", {}) or _ref_payload(ref)),
            "summary": {"tests": dict(report.get("summary", {}) or {})},
            "checks": {"tests": _ok_summary(report, "tests")},
            "tests": report,
            "errors": list(report.get("errors", []) or []),
        }

    report = check_package_report(ref, compare_path=compare_path, base_ref=base_ref)
    return {
        "ok": bool(report.get("ok")),
        "package": dict(report.get("package", {}) or _ref_payload(ref)),
        "summary": dict(report.get("summary", {}) or {}),
        "checks": _compact_full_checks(report),
        "check": report,
        "errors": _full_errors(report),
        "blockers": list(report.get("blockers", []) or []),
    }


def _starter_documents(
    *,
    package_id: str,
    description: str,
    entity: str,
    relation: str,
    primary_key: str,
    time_column: str,
    amount_column: str,
) -> dict[str, Any]:
    entity_title = _title(entity)
    package_title = _title(package_id)
    model_id = f"{entity}s" if not entity.endswith("s") else entity
    dimension_id = f"dimension.{package_id}_{entity}_event_type"
    temporal_role = f"temporal_role.{package_id}_{entity}_{time_column}"
    count_measure = f"measure.{package_id}.{entity}_count"
    amount_measure = f"measure.{package_id}.total_amount"
    amount_metric = f"metric.{package_id}.total_amount"

    return {
        "yaml": {
            "package.yml": {
                "schema_version": 1,
                "package": {
                    "id": package_id,
                    "namespace": package_id,
                    "name": package_id,
                    "description": description,
                    "warehouse": "duckdb",
                    "default_db": f"data/{package_id}.duckdb",
                    "schema_strict": True,
                    "environments": ["development", "staging", "production"],
                    "seed": {
                        "kind": "csv_dir_duckdb",
                        "source": f"data/{package_id}_csv",
                    },
                },
                "defaults": {
                    "dimension": {"groupable": True, "filterable": True},
                    "time": {
                        "timezone": "UTC",
                        "default_query_axis": False,
                        "supported_grains": ["day", "week", "month", "quarter", "year"],
                    },
                    "measure": {"subject_entity": "self", "aggregation_entity": "self"},
                    "relationship": {"traversal": ["forward", "reverse"]},
                },
            },
            "graph.yml": {
                "graph": {
                    "entities": {
                        entity: {
                            "label": entity_title,
                            "key": [primary_key],
                            "model": model_id,
                            "allowed_as_root": True,
                        }
                    }
                }
            },
            f"models/core/{model_id}.yml": {
                "model": {
                    "id": model_id,
                    "label": f"{entity_title} events",
                    "description": f"One row per {entity.replace('_', ' ')}.",
                    "relation": relation,
                    "entities": {entity: {}},
                    "times": {
                        time_column: {
                            "label": _title(time_column),
                            "column": time_column,
                            "kind": "timestamp",
                            "class": "event_time",
                            "default": True,
                        }
                    },
                    "dimensions": {
                        "event_type": {
                            "label": "Event type",
                            "kind": "categorical",
                            "domain": ["starter", "follow_up"],
                        }
                    },
                    "measures": {
                        f"{entity}_count": {
                            "label": f"{entity_title} count",
                            "kind": "entity_count",
                            "entity_key": primary_key,
                            "accumulation": {"kind": "event"},
                            "value_type": "count",
                            "meta": {
                                "owner_team": "analytics",
                                "review_priority": "medium",
                                "change_risk": "low",
                            },
                        },
                        "total_amount": {
                            "label": "Total amount",
                            "kind": "aggregate",
                            "expr": amount_column,
                            "default_agg": "sum",
                            "accumulation": {"kind": "flow"},
                            "value_type": "number",
                            "meta": {
                                "owner_team": "analytics",
                                "review_priority": "medium",
                                "change_risk": "low",
                            },
                        },
                    },
                }
            },
            "metrics/core/starter.yml": {
                "metrics": {
                    "total_amount": {
                        "label": "Total amount",
                        "description": (
                            f"Governed total amount for the {package_title} starter package."
                        ),
                        "kind": "aggregate",
                        "measure": "total_amount",
                        "value_type": "number",
                        "meta": {
                            "owner_team": "analytics",
                            "review_priority": "medium",
                            "change_risk": "low",
                        },
                    }
                }
            },
            "examples/core.yml": {
                "examples": {
                    "starter_amount_by_type": {
                        "question": "Total amount by event type",
                        "query": {
                            "version": 1,
                            "select": [
                                {"expression": {"metric": amount_metric}, "as": "total_amount"}
                            ],
                            "group_by": [dimension_id],
                            "time": {"temporal_role": temporal_role, "grain": "day"},
                            "order_by": [{"field": "time", "direction": "ASC"}],
                            "limit": 10,
                        },
                        "expected_shape": {
                            "columns": [
                                dimension_id,
                                f"{temporal_role}__day",
                                "total_amount",
                            ],
                            "min_rows": 1,
                        },
                    }
                }
            },
            "tests/core.yml": {
                "tests": {
                    "starter_count_returns_rows": {
                        "kind": "query_row_count_bounds",
                        "query": {
                            "version": 1,
                            "select": [
                                {"expression": {"measure": count_measure}, "as": "row_count"}
                            ],
                            "time": {"temporal_role": temporal_role, "grain": "day"},
                            "limit": 10,
                        },
                        "min_rows": 1,
                    },
                    "starter_amount_columns": {
                        "kind": "query_returns_columns",
                        "query": {
                            "version": 1,
                            "select": [
                                {"expression": {"measure": amount_measure}, "as": "total_amount"}
                            ],
                            "group_by": [dimension_id],
                            "limit": 10,
                        },
                        "columns": [dimension_id, "total_amount"],
                    },
                }
            },
        },
        "text": {
            ".gitignore": "data/*.duckdb\ndata/*.sqlite\ndata/*.sqlite3\n.compiled/\n",
        },
        "csv": (
            f"{primary_key},{time_column},event_type,{amount_column}\n"
            "1,2026-01-01T09:00:00,starter,100.0\n"
            "2,2026-01-02T09:00:00,follow_up,75.5\n"
        ),
    }


def _add_optional_reference_args(
    parser: argparse.ArgumentParser, package_choices: list[str]
) -> None:
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument(
        "--package",
        choices=package_choices,
        default="",
        help="Registered package id from configs/semantic_rails/.",
    )
    source.add_argument(
        "--path",
        default="",
        help="Path to a package directory or single-file YAML.",
    )


def _runtime_from_ref(ref: PackageReference) -> Runtime:
    if ref.package_id:
        return Runtime(ref.package_id)
    return Runtime.from_path(ref.source_path)


def _runtime_package_payload(runtime: Runtime, ref: PackageReference) -> dict[str, Any]:
    return {
        "id": runtime.package_id or ref.package_id or _package_id_from_yaml(ref.source_path),
        "source_path": ref.source_path,
        "warehouse": runtime.warehouse,
    }


def _catalog_kind(value: str) -> str:
    text = str(value or "all").strip().lower()
    aliases = {
        "entities": "entity",
        "dimensions": "dimension",
        "measures": "measure",
        "metrics": "metric",
        "segments": "segment",
        "temporal_role": "time",
        "temporal_roles": "time",
        "times": "time",
    }
    return aliases.get(text, text)


def _runtime_catalog_kind(selected: str) -> str:
    if selected == "all":
        return ""
    if selected == "time":
        return "temporal_role"
    return selected


def _catalog_objects(catalog: dict[str, Any], *, selected: str) -> list[dict[str, Any]]:
    keys = {
        "entity": ["entities"],
        "dimension": ["dimensions"],
        "measure": ["measures"],
        "metric": ["metrics"],
        "segment": ["segments"],
        "time": ["temporal_roles"],
        "all": ["entities", "dimensions", "measures", "metrics", "segments", "temporal_roles"],
    }[selected]
    objects: list[dict[str, Any]] = []
    for key in keys:
        singular = "time" if key == "temporal_roles" else key.rstrip("s")
        for entry in list(catalog.get(key, []) or []):
            if not isinstance(entry, dict):
                continue
            objects.append(
                {
                    "kind": str(entry.get("object_type") or entry.get("kind") or singular),
                    "id": str(entry.get("id", "") or ""),
                    "label": str(
                        entry.get("label")
                        or entry.get("display_name")
                        or entry.get("name")
                        or entry.get("id")
                        or ""
                    ),
                    "description": str(entry.get("description", "") or ""),
                    "root_entity": str(entry.get("root_entity", "") or ""),
                    "available": bool(entry.get("available", True)),
                }
            )
    objects.sort(key=lambda row: (row["kind"], row["id"]))
    return objects


def _planned_query(plan: dict[str, Any]) -> dict[str, Any]:
    best = dict(plan.get("best", {}) or {})
    query = best.get("query_ir")
    if isinstance(query, dict):
        return dict(query)
    next_payload = dict(plan.get("next", {}) or {})
    validate = dict(next_payload.get("validate", {}) or {})
    query = validate.get("query")
    return dict(query) if isinstance(query, dict) else {}


def _compact_plan(plan: dict[str, Any]) -> dict[str, Any]:
    best = dict(plan.get("best", {}) or {})
    resolved = []
    for row in list(best.get("resolved", []) or []):
        if isinstance(row, dict):
            resolved.append(
                {
                    "id": row.get("id", ""),
                    "label": row.get("label", ""),
                    "kind": row.get("object_type", ""),
                }
            )
    return {
        "ok": _payload_ok(plan),
        "pattern": best.get("pattern", ""),
        "validation_ok": best.get("validation_ok", False),
        "resolved": resolved,
        "rationale": list(best.get("rationale", []) or []),
    }


def _payload_ok(payload: dict[str, Any]) -> bool:
    if isinstance(payload.get("ok"), bool):
        return bool(payload["ok"])
    status = str(payload.get("status", "") or "").lower()
    return status in {"", "ok", "success"} and not list(payload.get("errors", []) or [])


def _ref_from_args(args: argparse.Namespace, *, allow_default: bool = True) -> PackageReference:
    package = str(getattr(args, "package", "") or "").strip()
    path = str(getattr(args, "path", "") or "").strip()
    if package or path or not allow_default:
        return resolve_package_reference(package_id=package, path=path)
    return _default_ref()


def _default_ref(*, package: str = "", path: str = "") -> PackageReference:
    package = str(package or "").strip()
    path = str(path or "").strip()
    if package or path:
        return resolve_package_reference(package_id=package, path=path)
    cwd_ref = _package_ref_from_cwd()
    if cwd_ref is not None:
        return cwd_ref
    local_path = resolve_local_package_path()
    if local_path:
        return resolve_package_reference(path=local_path)
    package_ids = sorted(list_package_paths().keys())
    if package_ids:
        return resolve_package_reference(package_id=package_ids[0])
    return resolve_package_reference()


def _package_ref_from_cwd() -> PackageReference | None:
    cwd = Path.cwd().resolve()
    registered = {
        str(Path(source_path).resolve()): package_id
        for package_id, source_path in list_package_paths().items()
    }
    for directory in [cwd, *cwd.parents]:
        package_yml = directory / "package.yml"
        if not package_yml.is_file():
            continue
        source = directory if (directory / "graph.yml").is_file() else package_yml
        source_path = str(source.resolve())
        return PackageReference(source_path=source_path, package_id=registered.get(source_path, ""))
    return None


def _registered_package_rows(*, with_status: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for package_id, source_path in sorted(list_package_paths().items()):
        rows.append(
            _package_row_from_source(
                source_path,
                origin="registered",
                package_id=package_id,
                with_status=with_status,
            )
        )
    return rows


def _package_row_from_source(
    source_path: str | Path,
    *,
    origin: str,
    package_id: str = "",
    with_status: bool,
) -> dict[str, Any]:
    source = str(Path(source_path).resolve())
    row: dict[str, Any] = {
        "id": package_id or _package_id_from_yaml(source),
        "source_path": source,
        "origin": origin,
        "layout": "directory" if Path(source).is_dir() else "single_file",
    }
    if with_status:
        ref = PackageReference(source_path=source, package_id=package_id)
        report, _ = parse_config_report(ref, progress=lambda _: None)
        row["ok"] = bool(report.get("ok"))
        row["summary"] = dict(report.get("summary", {}) or {})
        row["errors"] = list(report.get("errors", []) or [])
        row["warnings"] = list(report.get("warnings", []) or [])
        if not row["id"]:
            row["id"] = str(report.get("package", {}).get("id", "") or "")
    else:
        row["ok"] = True
    return row


def _package_id_from_yaml(source_path: str | Path) -> str:
    package_yml = Path(source_path)
    if package_yml.is_dir():
        package_yml = package_yml / "package.yml"
    try:
        payload = yaml.safe_load(package_yml.read_text(encoding="utf-8")) or {}
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    package = payload.get("package")
    if isinstance(package, dict):
        return str(package.get("id", "") or "")
    return ""


def _discover_package_sources(root: Path) -> list[str]:
    if not root.exists():
        raise SemanticLayerError("INVALID_CONFIG", f"Discovery root '{root}' does not exist")
    sources: list[str] = []
    for package_yml in sorted(root.rglob("package.yml")):
        if any(part in _EXCLUDED_DISCOVERY_DIRS for part in package_yml.parts):
            continue
        parent = package_yml.parent
        source = parent if (parent / "graph.yml").is_file() else package_yml
        sources.append(str(source))
    return sources


def _project_files(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in _EXCLUDED_DISCOVERY_DIRS for part in Path(relative).parts):
            continue
        files.append({"path": relative, "bytes": path.stat().st_size})
    return files


def _project_target(package_id: str, *, output: str, workspace_root: str) -> Path:
    if output:
        return Path(output).expanduser().resolve()
    root = Path(workspace_root).expanduser().resolve() if workspace_root else Path.cwd().resolve()
    if workspace_root and root.exists() and not root.is_dir():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "--workspace-root must be a directory",
            details={"workspace_root": str(root)},
        )
    configs_root = root / "configs" / "semantic_rails"
    if configs_root.is_dir():
        return configs_root / package_id
    return root / package_id


def _validate_project_target(target: Path) -> None:
    if target.is_symlink():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Target project directory must not be a symlink",
            details={"target": str(target)},
        )
    if target.exists() and not target.is_dir():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Target project path must be a directory",
            details={"target": str(target)},
        )


def _reject_symlink_tree(root: Path) -> None:
    if not root.exists():
        return
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Refusing to write through symlinks in the target project directory",
                details={"target": str(root), "symlink": str(path)},
            )


def _write_project_text(root: Path, path: Path, content: str) -> None:
    root_resolved = root.resolve(strict=False)
    path_resolved = path.resolve(strict=False)
    try:
        path_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Generated project files must stay inside the target directory",
            details={"target": str(root), "path": str(path)},
        ) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Refusing to overwrite a symlink",
            details={"path": str(path)},
        )
    path.write_text(content, encoding="utf-8")


def _python_check() -> dict[str, Any]:
    ok = sys.version_info >= (3, 11)
    return {
        "name": "python",
        "ok": ok,
        "required": True,
        "summary": sys.version.split()[0],
        "message": "Python 3.11 or newer is required.",
    }


def _module_check(module_name: str, *, package: str, optional: bool = False) -> dict[str, Any]:
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        return {
            "name": package,
            "ok": False,
            "required": not optional,
            "summary": "missing",
            "message": str(exc),
        }
    version = ""
    with _suppress_metadata_errors():
        version = metadata.version(package)
    return {
        "name": package,
        "ok": True,
        "required": not optional,
        "summary": version or "installed",
    }


class _suppress_metadata_errors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is metadata.PackageNotFoundError


def _uv_check() -> dict[str, Any]:
    path = shutil.which("uv")
    return {
        "name": "uv",
        "ok": bool(path),
        "required": False,
        "summary": path or "not found",
        "message": "uv is optional for installed use, but recommended for source checkouts.",
    }


def _parse_check(ref: PackageReference) -> dict[str, Any]:
    report, _ = parse_config_report(ref, progress=lambda _: None)
    return {
        "ok": bool(report.get("ok")),
        "summary": _parse_summary(report),
        "errors": list(report.get("errors", []) or []),
        "warnings": list(report.get("warnings", []) or [])[:5],
    }


def _runtime_check(ref: PackageReference) -> dict[str, Any]:
    report = validate_config_report(ref, progress=lambda _: None)
    return {
        "ok": bool(report.get("ok")),
        "summary": _validate_summary(report),
        "errors": list(report.get("errors", []) or []),
    }


def _full_check(ref: PackageReference) -> dict[str, Any]:
    report = check_package_report(ref)
    return {
        "ok": bool(report.get("ok")),
        "summary": dict(report.get("summary", {}) or {}),
        "blockers": list(report.get("blockers", []) or []),
    }


def _parse_summary(report: dict[str, Any]) -> dict[str, Any]:
    summary = dict(report.get("summary", {}) or {})
    return {
        "ok": bool(report.get("ok")),
        "entities": int(summary.get("entities", 0) or 0),
        "measures": int(summary.get("measures", 0) or 0),
        "metrics": int(summary.get("metric_recipes", summary.get("metrics", 0)) or 0),
        "warnings": int(summary.get("warnings", len(report.get("warnings", []) or [])) or 0),
        "errors": int(summary.get("errors", len(report.get("errors", []) or [])) or 0),
    }


def _validate_summary(report: dict[str, Any]) -> dict[str, Any]:
    summary = dict(report.get("summary", {}) or {})
    return {
        "ok": bool(report.get("ok")),
        "probes_total": int(summary.get("probes_total", 0) or 0),
        "passed": int(summary.get("passed", 0) or 0),
        "failed": int(summary.get("failed", 0) or 0),
    }


def _ok_summary(report: dict[str, Any], key: str) -> dict[str, Any]:
    summary = dict(report.get("summary", {}) or {})
    summary["ok"] = bool(report.get("ok"))
    if key == "examples":
        summary.setdefault("examples_total", 0)
    if key == "tests":
        summary.setdefault("tests_total", 0)
    return summary


def _compact_full_checks(report: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for name, summary in dict(report.get("summary", {}) or {}).items():
        if isinstance(summary, dict):
            checks[name] = dict(summary)
    return checks


def _full_errors(report: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for name, check in dict(report.get("checks", {}) or {}).items():
        if not isinstance(check, dict):
            continue
        for error in list(check.get("errors", []) or []):
            errors.append({"check": name, **dict(error)})
    return errors


def _ref_payload(ref: PackageReference) -> dict[str, Any]:
    return {
        "id": ref.package_id or _package_id_from_yaml(ref.source_path),
        "source_path": ref.source_path,
    }


def _setup_next_actions(packages: list[dict[str, Any]]) -> list[str]:
    actions = ["semantic-rails project list"]
    if packages:
        actions.append(f"semantic-rails project status --package {packages[0]['id']}")
    actions.extend(
        [
            "semantic-rails init my_package --yes",
            "semantic-rails profile init --package-path ./my_package",
            'semantic-rails ask --path ./my_package "total amount by event type" --run',
            "semantic-rails mcp setup --path ./my_package",
        ]
    )
    return actions


def _quote(path: Path | str) -> str:
    return shlex.quote(str(path))


def _status_next_actions(ref: PackageReference, validation: dict[str, Any]) -> list[str]:
    source = ref.package_id and f"--package {ref.package_id}" or f"--path {ref.source_path}"
    if not validation.get("ok"):
        return [f"semantic-rails project validate {source} --mode full --json"]
    return [
        f"semantic-rails catalog {source}",
        f"semantic-rails project validate {source}",
        f"semantic-rails mcp setup {source}",
    ]


def _ref_label(ref: PackageReference) -> str:
    return ref.package_id or ref.source_path


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _confirm(label: str, *, default: bool) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    value = input(f"{label}{suffix}: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "true", "1"}


def _prompt_choice(label: str, *, choices: list[str], default: str) -> str:
    choice_text = "/".join(choices)
    while True:
        value = input(f"{label} ({choice_text}) [{default}]: ").strip().lower() or default
        if value in choices:
            return value
        print(f"Choose one of: {choice_text}")


def _slug(value: str, *, fallback: str = "semantic_project") -> str:
    out = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or "")).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or fallback


def _title(value: str) -> str:
    return " ".join(part.capitalize() for part in str(value or "").replace("_", " ").split())


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _print_setup_report(report: dict[str, Any]) -> None:
    print("Semantic Rails setup")
    print(f"Repo: {report['repo_root']}")
    for check in report["checks"]:
        _print_check_line(check)
    print()
    print("Next commands:")
    for action in report["next_actions"]:
        print(f"  {action}")


def _print_debug_report(report: dict[str, Any]) -> None:
    print("Semantic Rails debug")
    print(f"Repo: {report['repo_root']}")
    for check in report["checks"]:
        _print_check_line(check)
    failing = [check for check in report["checks"] if not check.get("ok")]
    if failing:
        print()
        print("Fix the failed checks above, then rerun semantic-rails debug.")


def _print_objects(report: dict[str, Any]) -> None:
    package = report.get("package", {})
    print(
        f"{package.get('id') or '(package)'}: {report['count']} {report['resource_type']} object(s)"
    )
    if report.get("search"):
        print(f"Search: {report['search']}")
    for row in list(report.get("objects", []) or []):
        status = "" if row.get("available", True) else " unavailable"
        label = row.get("label") or row.get("id")
        print(f"  {row.get('kind')}: {row.get('id')}{status}")
        if label and label != row.get("id"):
            print(f"       {label}")
    if report.get("truncated"):
        print("  ... use --limit 0 or --json to see all objects")


def _print_ask_report(report: dict[str, Any]) -> None:
    package = report.get("package", {})
    print(f"Question: {report.get('question', '')}")
    print(f"Package: {package.get('id') or package.get('source_path') or '(unknown)'}")
    plan = dict(report.get("plan", {}) or {})
    print(f"Plan: {plan.get('pattern') or '(none)'}")
    resolved = list(plan.get("resolved", []) or [])
    if resolved:
        print("Resolved:")
        for row in resolved:
            print(f"  {row.get('kind')}: {row.get('id')} ({row.get('label')})")
    query = report.get("query")
    if query:
        print("Query IR:")
        print(json.dumps(query, indent=2, sort_keys=True, default=str))
    compiled = report.get("compile")
    if isinstance(compiled, dict):
        print("SQL:")
        print(compiled.get("sql", ""))
    result = report.get("result")
    if isinstance(result, dict):
        rows = list(result.get("rows", []) or [])
        print(f"Rows: {result.get('row_count', len(rows))}")
        _print_rows(rows[:20])
    errors = list(report.get("errors", []) or [])
    if errors:
        print("Errors:")
        for error in errors[:5]:
            print(f"  {error.get('code', 'ERROR')}: {error.get('message', error)}")


def _print_project_list(report: dict[str, Any]) -> None:
    print(f"Semantic Rails projects ({report['count']})")
    for row in report["packages"]:
        status = "ok" if row.get("ok") else "fail"
        label = row.get("id") or "(unknown)"
        origin = row.get("origin", "")
        print(f"  [{status}] {label} ({origin})")
        print(f"       {row.get('source_path')}")
        summary = row.get("summary")
        if isinstance(summary, dict) and summary:
            metrics = summary.get("metric_recipes", summary.get("metrics", 0))
            print(
                "       "
                f"entities={summary.get('entities', 0)} "
                f"measures={summary.get('measures', 0)} "
                f"metrics={metrics} "
                f"warnings={summary.get('warnings', 0)} "
                f"errors={summary.get('errors', 0)}"
            )


def _print_project_created(report: dict[str, Any]) -> None:
    print("Created Semantic Rails project")
    print(f"Package: {report['package_id']}")
    print(f"Path: {report['project_path']}")
    print("Files:")
    for path in report["changed_files"]:
        print(f"  {path}")
    checks = report.get("checks", {})
    if checks:
        print("Checks:")
        for name, check in checks.items():
            if isinstance(check, dict):
                _print_named_check(name, check)
    print("Next commands:")
    for action in report["next_actions"]:
        print(f"  {action}")


def _print_project_status(report: dict[str, Any]) -> None:
    package = report.get("package", {})
    print(f"Semantic Rails project: {package.get('id') or '(unknown)'}")
    print(f"Path: {report['source_path']}")
    print(f"Layout: {report['layout']}")
    print(f"Files: {len(report['files'])}")
    checks = report.get("checks", {})
    if checks:
        print("Checks:")
        for name, check in checks.items():
            if isinstance(check, dict):
                _print_named_check(name, check)
    print("Next commands:")
    for action in report["next_actions"]:
        print(f"  {action}")


def _print_project_validation(report: dict[str, Any]) -> None:
    package = report.get("package", {})
    print(f"Validation: {package.get('id') or package.get('source_path') or '(unknown)'}")
    checks = report.get("checks", {})
    for name, check in checks.items():
        if isinstance(check, dict):
            _print_named_check(name, check)
    warnings = _authoring_warning_messages(report)
    if warnings:
        print("Warnings:")
        for warning in warnings[:10]:
            print(f"  {warning}")
        if len(warnings) > 10:
            print(f"  ... {len(warnings) - 10} more warning(s)")
    errors = list(report.get("errors", []) or [])
    if errors:
        print("Errors:")
        for error in errors[:10]:
            message = error.get("message") or error
            check = error.get("check")
            prefix = f"{check}: " if check else ""
            print(f"  {prefix}{message}")


def _print_profile_report(report: dict[str, Any]) -> None:
    print("Semantic Rails profile")
    print(f"Path: {report.get('path')}")
    print(f"Configured: {report.get('exists')}")
    print(f"Profile: {report.get('active_profile') or '(none)'}")
    print(f"Target: {report.get('active_target') or '(none)'}")
    print(f"Package: {report.get('resolved_package_path') or '(none)'}")
    profiles = ", ".join(list(report.get("profiles", []) or [])) or "(none)"
    if profiles != "(none)":
        print(f"Profiles: {profiles}")
    print("Scope: local CLI only")


def _print_check_line(check: dict[str, Any]) -> None:
    status = "ok" if check.get("ok") else ("warn" if not check.get("required", True) else "fail")
    print(f"  [{status}] {check['name']}: {check.get('summary', '')}")
    if not check.get("ok") and check.get("message"):
        print(f"       {check['message']}")


def _print_named_check(name: str, check: dict[str, Any]) -> None:
    status = "ok" if check.get("ok", True) else "fail"
    bits = [f"[{status}] {name}"]
    for key in (
        "entities",
        "measures",
        "metrics",
        "probes_total",
        "passed",
        "failed",
        "examples_total",
        "tests_total",
        "warnings",
        "errors",
    ):
        if key in check:
            bits.append(f"{key}={check[key]}")
    print("  " + " ".join(bits))


def _print_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(str(key))
    widths = {
        col: min(
            40,
            max(len(col), *(len(str(row.get(col, ""))) for row in rows)),
        )
        for col in columns
    }
    header = " | ".join(col.ljust(widths[col]) for col in columns)
    print(header)
    print("-+-".join("-" * widths[col] for col in columns))
    for row in rows:
        print(
            " | ".join(str(row.get(col, ""))[: widths[col]].ljust(widths[col]) for col in columns)
        )
