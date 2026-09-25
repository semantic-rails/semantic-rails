"""Human developer commands: setup, debug, ls, ask, repl, profile and project."""

from __future__ import annotations

import argparse
import sys

from ...errors import SemanticLayerError
from ...local_config import init_local_profile, local_profile_report
from ..common import (
    _add_optional_reference_args,
    _print_json,
    _prompt,
    _prompts_allowed,
    _ref_from_args,
    _slug,
    _title,
)
from ..output import (
    _print_ask_report,
    _print_debug_report,
    _print_objects,
    _print_profile_report,
    _print_project_created,
    _print_project_list,
    _print_project_status,
    _print_project_validation,
    _print_setup_report,
)
from ..reports import (
    CATALOG_KINDS,
    PROJECT_CHECK_MODES,
    ask_report,
    list_objects_report,
    project_list_report,
    project_status_report,
    project_validation_report,
    setup_report,
)
from ..scaffold import _project_target, create_project_report
from ..setup_wizard import cmd_setup_interactive


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
        help=(
            "Row cap for --run results (default: 20). 0 removes this cap; a limit the "
            "planned query carries itself still applies."
        ),
    )
    p_ask.add_argument("--json", action="store_true", help="Print a JSON report.")
    p_ask.set_defaults(func=cmd_ask, human_cli=True)

    p_repl = sub.add_parser(
        "repl",
        description=(
            "Open an interactive Semantic Rails command loop. Without a package it starts "
            "on a home screen: open a project found here, create one, import a dbt project, "
            "or try the bundled sample. In a terminal it uses "
            "arrow-key pickers when semantic-rails[repl] is installed; set "
            "SEMANTIC_RAILS_UI=plain for line prompts (e.g. with a screen reader)."
        ),
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
    ref = _ref_from_args(args, interactive=_prompts_allowed(args))
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
    # Settle which package answers before asking anything else.
    ref = _ref_from_args(args, interactive=_prompts_allowed(args))
    if not question and sys.stdin.isatty() and not args.json:
        question = input("Question: ").strip()
    if not question:
        raise SemanticLayerError(
            "INVALID_QUERY",
            "Provide a question, for example: semantic-rails ask 'monthly revenue by store'",
            details={"path": "question"},
        )
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
    from ...repl.shell import run_interactive_shell  # the REPL imports this package

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
    ref = _ref_from_args(args, interactive=_prompts_allowed(args))
    report = project_status_report(ref, checks=args.checks)
    if args.json:
        _print_json(report)
    else:
        _print_project_status(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_project_validate(args: argparse.Namespace) -> None:
    ref = _ref_from_args(args, interactive=_prompts_allowed(args))
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
