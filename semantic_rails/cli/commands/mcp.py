"""``semantic-rails mcp`` commands: servers, doctor, setup and client configs."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ...errors import SemanticLayerError
from ...mcp import SemanticLayerMCPAdapter
from ...mcp_manager import (
    managed_mcp_lifecycle_report,
    mcp_client_config_report,
    mcp_status_report,
    start_mcp_http_server,
    stop_mcp_http_server,
)
from ...mcp_server import serve_http as serve_mcp_http
from ...mcp_server import serve_stdio as serve_mcp_stdio
from ...runtime import Runtime
from ..common import (
    _confirm,
    _is_bundled_ref,
    _optional_ref_from_args,
    _print,
    _required_ref_from_args,
    _runtime_from_package_or_path,
    _runtime_from_ref,
    _source_arg_from_ref,
    _source_arg_from_runtime,
)
from ..output import _package_display

MCP_REQUIRED_TOOLS = (
    "capabilities",
    "catalog",
    "discover",
    "inspect",
    "build-options",
    "valid-values",
    "plan",
    "validate",
    "compile",
    "execute",
)


def _mcp_tool_check(runtime: Runtime) -> dict[str, Any]:
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        tools = adapter.list_tools()
    finally:
        adapter.close()
    tool_names = sorted(str(tool.get("name", "")) for tool in tools if tool.get("name"))
    missing = [name for name in MCP_REQUIRED_TOOLS if name not in tool_names]
    return {
        "adapter": "ok",
        "tool_count": len(tools),
        "required_tools_present": not missing,
        "missing_required_tools": missing,
    }


def cmd_mcp_stdio(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        adapter.list_tools()
        serve_mcp_stdio(adapter)
    finally:
        adapter.close()


def cmd_mcp_http(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        adapter.list_tools()
        serve_mcp_http(adapter, host=args.host, port=args.port)
    finally:
        adapter.close()


def cmd_mcp_doctor(args: argparse.Namespace) -> None:
    runtime = _runtime_from_package_or_path(args)
    mcp = _mcp_tool_check(runtime)
    source_arg = _source_arg_from_runtime(
        runtime,
        prefer_path=bool(str(getattr(args, "path", "") or "").strip()),
    )
    lifecycle = managed_mcp_lifecycle_report()
    next_commands = [f"semantic-rails mcp setup {source_arg}"]
    if lifecycle["supported"]:
        next_commands.extend(
            [
                f"semantic-rails mcp status {source_arg}",
                f"semantic-rails mcp start {source_arg} --port 8091",
                "curl -s http://127.0.0.1:8091/health",
                f"semantic-rails mcp stop {source_arg}",
            ]
        )
    else:
        next_commands.extend(
            [
                f"semantic-rails mcp setup {source_arg} --client both --mcp query --install --yes",
                f"semantic-rails mcp stdio {source_arg}",
                f"semantic-rails mcp http {source_arg} --host 127.0.0.1 --port 8091",
            ]
        )
    payload = {
        "ok": bool(mcp["required_tools_present"]),
        "package": {
            "id": runtime.package_id,
            "source_path": runtime.source_path,
            "warehouse": runtime.warehouse,
        },
        "mcp": mcp,
        "managed_lifecycle": lifecycle,
        "next_commands": next_commands,
    }
    _print(payload)
    if not payload["ok"]:
        raise SystemExit(1)


def cmd_mcp_setup(args: argparse.Namespace) -> None:
    ref = _required_ref_from_args(args)
    install = bool(getattr(args, "install", False))
    if install and not getattr(args, "yes", False):
        if getattr(args, "json", False) or not sys.stdin.isatty():
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Pass --yes with --install to write Claude or Codex config files.",
            )
        install = _confirm("Write Claude/Codex MCP config files?", default=False)

    runtime = _runtime_from_ref(ref)
    mcp = _mcp_tool_check(runtime)
    config = mcp_client_config_report(
        ref,
        client=args.client,
        mcp=args.mcp,
        workspace_root=args.workspace_root,
        install=install,
    )
    source_arg = _source_arg_from_ref(ref)
    next_commands = [
        "Restart the selected MCP client so it reloads its config."
        if install
        else (
            f"semantic-rails mcp setup {source_arg} --client {args.client} "
            f"--mcp {args.mcp} --install --yes"
        ),
        f"semantic-rails mcp doctor {source_arg}",
        f"semantic-rails mcp status {source_arg}",
    ]
    payload = {
        "ok": bool(mcp["required_tools_present"] and config.get("ok")),
        "mode": "install" if install else "preview",
        "package": {
            "id": runtime.package_id,
            "source_path": runtime.source_path,
            "warehouse": runtime.warehouse,
            "bundled": _is_bundled_ref(ref),
        },
        "mcp": mcp,
        "client_config": {
            "client": config["client"],
            "mcp": config["mcp"],
            "workspace_root": config["workspace_root"],
            "servers": config["servers"],
            "previews": config["previews"],
            "installed": config["installed"],
        },
        "next_commands": next_commands,
    }
    if getattr(args, "json", False):
        _print(payload)
    else:
        _print_mcp_setup_report(payload)
    if not payload["ok"]:
        raise SystemExit(1)


def _print_mcp_setup_report(payload: dict[str, Any]) -> None:
    package = dict(payload.get("package", {}) or {})
    mcp = dict(payload.get("mcp", {}) or {})
    config = dict(payload.get("client_config", {}) or {})
    print("Semantic Rails MCP setup")
    print(f"Package: {_package_display(package)}")
    print(
        f"MCP check: {'ok' if payload.get('ok') else 'failed'} ({mcp.get('tool_count', 0)} tools)"
    )
    print(f"Mode: {payload.get('mode')}")
    print()
    print("Config files:")
    for client, preview in dict(config.get("previews", {}) or {}).items():
        print(f"  {client}: {preview.get('path')}")
    installed = dict(config.get("installed", {}) or {})
    if installed:
        print()
        print("Installed:")
        for client, report in installed.items():
            print(f"  {client}: {report.get('path')}")
    print()
    print("Next commands:")
    for command in list(payload.get("next_commands", []) or []):
        print(f"  {command}")


def cmd_mcp_start(args: argparse.Namespace) -> None:
    ref = _required_ref_from_args(args)
    report = start_mcp_http_server(
        ref,
        name=args.name,
        host=args.host,
        port=args.port,
    )
    _print(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_mcp_stop(args: argparse.Namespace) -> None:
    _print(stop_mcp_http_server(name=args.name, ref=_optional_ref_from_args(args)))


def cmd_mcp_status(args: argparse.Namespace) -> None:
    _print(mcp_status_report(_optional_ref_from_args(args)))


def cmd_mcp_client_config(args: argparse.Namespace) -> None:
    ref = _required_ref_from_args(args)
    if args.install and not args.yes:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Pass --yes with --install to write Claude or Codex config files.",
        )
    _print(
        mcp_client_config_report(
            ref,
            client=args.client,
            mcp=args.mcp,
            workspace_root=args.workspace_root,
            install=args.install,
        )
    )
