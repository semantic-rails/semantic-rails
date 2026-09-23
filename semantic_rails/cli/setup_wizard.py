"""The interactive ``semantic-rails setup --interactive`` wizard."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..config_validation import PackageReference, resolve_package_reference
from ..errors import SemanticLayerError
from ..local_config import init_local_profile
from ..mcp_manager import (
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORT,
    managed_mcp_lifecycle_report,
    mcp_client_config_report,
    start_mcp_http_server,
    stop_mcp_http_server,
)
from .common import (
    _confirm,
    _package_ref_from_cwd,
    _print_json,
    _prompt,
    _prompt_choice,
    _quote,
    _ref_from_args,
    _ref_label,
)
from .output import _print_project_created, _print_project_validation
from .reports import project_validation_report
from .scaffold import create_project_report


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
