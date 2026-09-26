"""Shared CLI plumbing: package selection, runtime loading, argument helpers,
prompts, terminal capabilities and small formatting utilities.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

from ..config import list_package_paths, package_root_for_source
from ..config_validation import PackageReference, resolve_package_reference
from ..errors import SemanticLayerError
from ..local_config import resolve_local_package_path
from ..runtime import Runtime

# The only bundled package offered when a person runs a command without choosing one.
DEMO_PACKAGE_ID = "jaffle_shop"


# Packages the engine ships with sample data. Other registered packages (for
# example a contributor's own under configs/semantic_rails/) are not samples.
SAMPLE_PACKAGE_IDS = frozenset({DEMO_PACKAGE_ID, "tpch_sf1_showcase"})


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


def _ref_from_args(
    args: argparse.Namespace, *, allow_default: bool = True, interactive: bool = False
) -> PackageReference:
    package = str(getattr(args, "package", "") or "").strip()
    path = str(getattr(args, "path", "") or "").strip()
    if package or path or not allow_default:
        return resolve_package_reference(package_id=package, path=path)
    return default_package_ref(interactive=interactive)


def _default_ref(
    *, package: str = "", path: str = "", interactive: bool = False
) -> PackageReference:
    package = str(package or "").strip()
    path = str(path or "").strip()
    if package or path:
        return resolve_package_reference(package_id=package, path=path)
    return default_package_ref(interactive=interactive)


def default_package_ref(*, interactive: bool = False) -> PackageReference:
    """Resolve the package for a command that names none with ``--package``/``--path``.

    A ``package.yml`` in the working directory (or a parent) wins, then the
    local profile. Nothing else is implicit: answering from the bundled sample
    package takes ``--package jaffle_shop``, or a confirmation at an
    interactive terminal. Otherwise the command stops with guidance.
    """

    chosen = chosen_package_ref()
    if chosen is not None:
        return chosen
    demo_available = DEMO_PACKAGE_ID in list_package_paths()
    if interactive and demo_available and _confirm_demo_package():
        return resolve_package_reference(package_id=DEMO_PACKAGE_ID)
    raise _no_package_selected_error(demo_available=demo_available)


def chosen_package_ref() -> PackageReference | None:
    """The package in the working directory or a parent, then the local profile's, else None."""

    cwd_ref = _package_ref_from_cwd()
    if cwd_ref is not None:
        return cwd_ref
    local_path = resolve_local_package_path()
    return resolve_package_reference(path=local_path) if local_path else None


def _confirm_demo_package() -> bool:
    _, color = _repl_capabilities()
    print()
    print(_repl_color("No package selected.", "1;33", enabled=color))
    print("  There is no --package or --path, no package.yml in this directory or")
    print("  its parents, and no local profile.")
    print(f"  The bundled `{DEMO_PACKAGE_ID}` package holds sample data, not yours.")
    try:
        return _confirm(f"Use the bundled `{DEMO_PACKAGE_ID}` sample package?", default=False)
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _no_package_selected_error(*, demo_available: bool) -> SemanticLayerError:
    choices = [
        "pass --path <package-dir>",
        "run the command inside a package directory",
        "set a default: semantic-rails profile init --package-path <package-dir>",
        "create a package: semantic-rails init my_package",
    ]
    if demo_available:
        choices.append(f"try the bundled sample data: --package {DEMO_PACKAGE_ID}")
    return SemanticLayerError(
        "INVALID_CONFIG",
        "No package selected. Choose one:\n" + "\n".join(f"  - {choice}" for choice in choices),
        details={"reason": "no_package_selected"},
    )


def _is_terminal(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _prompts_allowed(args: argparse.Namespace) -> bool:
    """True when a human command may ask the person at the terminal a question."""

    return not getattr(args, "json", False) and _is_terminal(sys.stdin) and _is_terminal(sys.stdout)


def _is_bundled_ref(ref: PackageReference) -> bool:
    """True when the reference is a shipped sample package, however it was selected."""

    root = Path(package_root_for_source(ref.source_path)).resolve()
    return any(
        Path(package_root_for_source(path)).resolve() == root
        for package_id, path in list_package_paths().items()
        if package_id in SAMPLE_PACKAGE_IDS
    )


def _package_ref_from_cwd() -> PackageReference | None:
    cwd = Path.cwd().resolve()
    return next(filter(None, map(_package_ref_at, [cwd, *cwd.parents])), None)


def _package_ref_at(directory: Path) -> PackageReference | None:
    """The package whose ``package.yml`` is in ``directory``, if there is one."""

    package_yml = directory / "package.yml"
    if not package_yml.is_file():
        return None
    source_path = str((directory if (directory / "graph.yml").is_file() else package_yml).resolve())
    registered = {
        str(Path(source).resolve()): package_id
        for package_id, source in list_package_paths().items()
    }
    return PackageReference(source_path=source_path, package_id=registered.get(source_path, ""))


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


def _ref_payload(ref: PackageReference) -> dict[str, Any]:
    return {
        "id": ref.package_id or _package_id_from_yaml(ref.source_path),
        "source_path": ref.source_path,
    }


def _quote(path: Path | str) -> str:
    return shlex.quote(str(path))


def _ref_label(ref: PackageReference) -> str:
    return ref.package_id or ref.source_path


_BUNDLED_NOTE = " (bundled sample package, not your data)"


def _ref_display(ref: PackageReference) -> str:
    return _ref_label(ref) + (_BUNDLED_NOTE if _is_bundled_ref(ref) else "")


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


_TITLE_ACRONYMS = {"mom": "MoM", "qoq": "QoQ", "wow": "WoW", "yoy": "YoY"}
_TITLE_ACRONYMS |= {word: word.upper() for word in ("mtd", "qtd", "ytd")}


def _title(value: str) -> str:
    return " ".join(
        _TITLE_ACRONYMS.get(part.lower(), part.capitalize())
        for part in str(value or "").replace("_", " ").split()
    )


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _parse_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("@"):
        with open(text[1:], encoding="utf-8") as f:
            text = f.read()
    return dict(json.loads(text) or {})


def _with_ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure the CLI JSON envelope carries a boolean ``ok`` field.

    Mirrors the HTTP envelope contract (documented in docs/QUERY_API.md):
    every response must expose ``ok`` as ``True`` on success and ``False``
    on error. Without this, ``jq '.ok'`` against CLI output returns
    ``null`` for successful calls.
    """
    if not isinstance(payload, dict):
        return payload
    existing = payload.get("ok")
    if isinstance(existing, bool):
        return payload
    out = dict(payload)
    status = str(out.get("status", "") or "").lower()
    errors = list(out.get("errors", []) or [])
    if status == "error" or errors:
        out["ok"] = False
    else:
        # Success path: status was "ok" or unset on a normal payload.
        out["ok"] = True
    return out


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(_with_ok(payload), indent=2, sort_keys=True, default=str))


def _print_stderr(message: str) -> None:
    print(message, file=sys.stderr)


def _policy_context_from_args(args: argparse.Namespace) -> dict[str, Any]:
    environment = str(getattr(args, "environment", "") or "")
    audience = str(getattr(args, "audience", "") or "")
    return {
        key: value
        for key, value in {"environment": environment, "audience": audience}.items()
        if value
    }


def _query_with_policy_context(
    query: dict[str, Any] | None, args: argparse.Namespace
) -> dict[str, Any] | None:
    policy_context = _policy_context_from_args(args)
    if not policy_context:
        return dict(query or {}) if query is not None else None
    payload = dict(query or {})
    payload["policy_context"] = policy_context
    return payload


def _query_payload_from_args(args: argparse.Namespace) -> dict[str, Any]:
    payload = _query_with_policy_context(_parse_json(args.query_json), args) or {}
    verbosity = str(getattr(args, "verbosity", "") or "").strip()
    if verbosity:
        payload["verbosity"] = verbosity
    sql_profile = str(getattr(args, "sql_profile", "") or "").strip()
    if sql_profile:
        payload["sql_profile"] = sql_profile
    return payload


def _add_policy_context_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--environment",
        default="",
        help="Policy environment to evaluate against (e.g. 'dev', 'prod'). Optional.",
    )
    parser.add_argument(
        "--audience",
        default="",
        help="Policy audience to evaluate against (e.g. 'analyst', 'agent'). Optional.",
    )


def _add_response_detail_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--verbosity",
        choices=["minimal", "compact", "full"],
        default="",
        help=(
            "Response detail for validate/compile/query. Use 'minimal' for row-focused "
            "CLI output; omit for the runtime default."
        ),
    )
    parser.add_argument(
        "--sql-profile",
        choices=["audit", "compact", "debug", "off"],
        default="",
        help=(
            "SQL/explain profile for validate/compile/query. Use 'off' to suppress SQL "
            "metadata in row-focused CLI output."
        ),
    )


def _runtime_from_package_or_path(args: argparse.Namespace) -> Runtime:
    return _runtime_from_ref(_package_ref_from_args(args))


def _add_package_or_path_args(parser: argparse.ArgumentParser, package_choices: list[str]) -> None:
    parser.add_argument(
        "--package",
        choices=package_choices,
        default="",
        help="Registered package id from configs/semantic_rails/.",
    )
    parser.add_argument(
        "--path",
        default="",
        help="Path to a package directory or single-file YAML. Overrides --package when set.",
    )


def _add_optional_package_or_path_args(
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


def _required_ref_from_args(args: argparse.Namespace) -> PackageReference:
    return _package_ref_from_args(args)


def _optional_ref_from_args(args: argparse.Namespace) -> PackageReference | None:
    package = str(getattr(args, "package", "") or "").strip()
    path = str(getattr(args, "path", "") or "").strip()
    if not package and not path:
        return None
    return resolve_package_reference(package_id=package, path=path)


def _default_mcp_ref() -> PackageReference:
    return _default_cli_ref()


def _default_cli_ref() -> PackageReference:
    # Runtime commands print JSON (and `mcp stdio` owns stdin), so they never
    # prompt: without a chosen package they stop with guidance.
    return default_package_ref(interactive=False)


def _package_ref_from_args(args: argparse.Namespace) -> PackageReference:
    """Resolve the package once for every package-aware CLI command.

    Explicit ``--path``/``--package`` always wins. Otherwise use the
    nearest package directory, then the opted-in local profile. Nothing
    falls back to a bundled package: without a choice the command fails
    with guidance instead of answering from sample data.
    """

    package = str(getattr(args, "package", "") or "").strip()
    path = str(getattr(args, "path", "") or "").strip()
    if package or path:
        return resolve_package_reference(package_id=package, path=path)
    return _default_cli_ref()


def _source_arg_from_runtime(runtime: Runtime, *, prefer_path: bool) -> str:
    if prefer_path:
        return f"--path {shlex.quote(runtime.source_path)}"
    return f"--package {shlex.quote(runtime.package_id)}"


def _source_arg_from_ref(ref: PackageReference) -> str:
    if ref.package_id:
        return f"--package {shlex.quote(ref.package_id)}"
    return f"--path {shlex.quote(ref.source_path)}"


def _add_config_reference_args(parser: argparse.ArgumentParser, package_choices: list[str]) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--package",
        choices=package_choices,
        help="Registered package id from configs/semantic_rails/. Mutually exclusive with --path.",
    )
    source.add_argument(
        "--path",
        default="",
        help="Path to a package directory or single-file YAML. Mutually exclusive with --package.",
    )
