"""The interactive ``semantic-rails repl`` command loop, its banner, prompt and help."""

from __future__ import annotations

import sys
from pathlib import Path

from ..cli.common import (
    _default_ref,
    _is_terminal,
    _ref_display,
    _repl_capabilities,
    _repl_color,
    _runtime_from_ref,
)
from ..cli.output import (
    _authoring_error_messages,
    _print_ask_report,
    _print_objects,
    _print_project_list,
    _print_project_status,
    _print_project_validation,
)
from ..cli.reports import (
    CATALOG_KINDS,
    PROJECT_CHECK_MODES,
    ask_report,
    list_objects_report,
    project_list_report,
    project_status_report,
    project_validation_report,
)
from ..config import package_root_for_source
from ..config_validation import PackageReference, resolve_package_reference
from ..errors import SemanticLayerError
from .authoring import Undoable, _authoring_warehouse, _run_authoring_flow
from .backend import current_backend, pickers_available
from .prompts import _author_confirm, _AuthoringCancelled


def run_interactive_shell(*, package: str = "", path: str = "") -> None:
    current_ref = _default_ref(
        package=package,
        path=path,
        interactive=_is_terminal(sys.stdin) and _is_terminal(sys.stdout),
    )
    undo_stack: list[Undoable] = []
    current_backend()  # an unusable SEMANTIC_RAILS_UI fails here, before the banner
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
        print(f"Using {_ref_display(current_ref)}")
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
    print(f"  package  {_ref_display(current_ref)}")
    print("  help     type help for the timetable · exit when done")
    print(f"  prompts  {_prompt_style()}")
    print()


def _prompt_style() -> str:
    if current_backend().name == "pickers":
        return "arrow-key pickers · SEMANTIC_RAILS_UI=plain for line prompts"
    if pickers_available():
        return "line prompts"
    return "line prompts · pip install 'semantic-rails[repl]' for arrow-key pickers"


def _repl_prompt(current_ref: PackageReference) -> str:
    visual, _ = _repl_capabilities()
    if not visual:
        return "semantic-rails> "
    label = current_ref.package_id or Path(current_ref.source_path).name or current_ref.source_path
    return f"semantic-rails [{label}] › "


def _handle_repl_line(
    line: str,
    current_ref: PackageReference,
    *,
    undo_stack: list[Undoable] | None = None,
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
        print(f"Using {_ref_display(current_ref)}")
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
            print(_operational_notice(current_ref, mode))
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


def _operational_notice(ref: PackageReference, mode: str) -> str:
    """Name the selected database and describe the runtime's no-replacement rule."""

    try:
        runtime = _runtime_from_ref(ref)
    except (OSError, SemanticLayerError):
        warehouse = _authoring_warehouse(ref)
        return (
            f"Operational check: {mode} may query the {warehouse} warehouse. "
            "Package details could not be read; validation will report why."
        )
    try:
        if runtime.warehouse != "duckdb":
            return (
                f"Operational check: {mode} may query or refresh the {runtime.warehouse} warehouse."
            )
        database = Path(runtime.db_path)
        shown = _shown_path(database)
        seed = runtime.config.package.seed
        if database.is_symlink() and not database.exists():
            return (
                f"Operational check: {mode} found a broken link at the DuckDB file {shown}. "
                "Validation reports it and does not build through the link."
            )
        if not database.exists():
            if seed.kind == "external":
                return (
                    f"Operational check: {mode} found no DuckDB file at {shown}. "
                    "The package uses an external seed, so validation reports the missing file "
                    "and does not create it."
                )
            return (
                f"Operational check: {mode} may create the missing DuckDB file {shown} "
                f"from the package seed ({seed.kind} {seed.source}), then query it. "
                "Validation reports a missing or unusable seed."
            )
        return (
            f"Operational check: {mode} queries the existing DuckDB file {shown}. "
            "It is never rebuilt or replaced; validation reports missing relations "
            "or an unreadable file."
        )
    finally:
        runtime.close()


def _shown_path(path: Path) -> str:
    try:
        return f"./{path.relative_to(Path.cwd().resolve()).as_posix()}"
    except ValueError:
        return str(path)


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
