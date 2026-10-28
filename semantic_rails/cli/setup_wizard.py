"""The interactive ``semantic-rails setup --interactive`` wizard."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..config_validation import PackageReference, resolve_package_reference
from ..errors import SemanticLayerError
from ..local_config import init_local_profile
from ..mcp_manager import mcp_client_config_report
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
        f"semantic-rails serve --path {_quote(ref.source_path)} --port 8091",
    ]:
        print(f"  {command}")


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
