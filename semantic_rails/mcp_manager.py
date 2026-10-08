"""Local MCP client-configuration helpers.

These helpers are deliberately local-developer conveniences. They do not
change the MCP protocol server, hosted deployment behavior, or package format.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .atomic_files import atomic_write_bytes
from .config import package_root_for_source
from .config_validation import PackageReference
from .errors import SemanticLayerError

# "both" stays Claude Desktop and Codex (the first two), as for MCP_KINDS.
CLIENTS = ("claude", "codex", "claude-code", "cursor", "both")
MCP_KINDS = ("query", "architect", "both")


def mcp_client_config_report(
    ref: PackageReference,
    *,
    client: str = "both",
    mcp: str = "both",
    workspace_root: str = "",
    install: bool = False,
) -> dict[str, Any]:
    selected_clients = _selected(client, CLIENTS, field="client")
    selected_mcp = _selected(mcp, MCP_KINDS, field="mcp")
    workspace = Path(workspace_root or _workspace_root_for_ref(ref)).expanduser().resolve()
    servers = _client_servers(ref, selected_mcp=selected_mcp, workspace_root=workspace)
    previews = {
        selected_client: _client_preview(selected_client, servers)
        for selected_client in selected_clients
    }
    installed = {}
    if install:
        for selected_client in selected_clients:
            installed[selected_client] = _install_client_config(selected_client, servers)
    return {
        "ok": True,
        "client": client,
        "mcp": mcp,
        "package": {"id": ref.package_id, "source_path": ref.source_path},
        "workspace_root": str(workspace),
        "servers": servers,
        "previews": previews,
        "installed": installed,
    }


def claude_config_path() -> Path:
    override = os.environ.get("SEMANTIC_RAILS_CLAUDE_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def codex_config_path() -> Path:
    override = os.environ.get("SEMANTIC_RAILS_CODEX_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex" / "config.toml"


def cursor_config_path() -> Path:
    override = os.environ.get("SEMANTIC_RAILS_CURSOR_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cursor" / "mcp.json"


def claude_code_config_path() -> Path:
    """Where Claude Code keeps user-scope servers; its CLI writes it, not us."""

    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home()) / ".claude.json"


def _claude_code_add(claude: str, servers: dict[str, dict[str, Any]]) -> list[list[str]]:
    """Claude Code owns its config file, so user-scope servers go through its CLI."""

    return [
        [claude, "mcp", "add-json", "--scope", "user", name, json.dumps(config)]
        for name, config in servers.items()
    ]


def _client_preview(client: str, servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if client in ("claude", "cursor"):
        path = claude_config_path() if client == "claude" else cursor_config_path()
        return {"path": str(path), "mcpServers": servers}
    if client == "claude-code":
        commands = [shlex.join(command) for command in _claude_code_add("claude", servers)]
        return {"path": str(claude_code_config_path()), "commands": commands}
    if client == "codex":
        blocks = [_codex_toml_block(name, config) for name, config in servers.items()]
        return {"path": str(codex_config_path()), "toml": "\n".join(blocks).strip() + "\n"}
    raise SemanticLayerError("INVALID_CONFIG", f"Unsupported MCP client '{client}'")


def _install_client_config(client: str, servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if client in ("claude", "cursor"):
        path = claude_config_path() if client == "claude" else cursor_config_path()
        data: dict[str, Any] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            if not isinstance(data, dict):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{client.title()} config must be a JSON object: {path}",
                    details={"path": str(path)},
                )
        mcp_servers = dict(data.get("mcpServers", {}) or {})
        mcp_servers.update(servers)
        data["mcpServers"] = mcp_servers
        atomic_write_bytes(
            path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"), mode=0o600
        )
        report: dict[str, Any] = {"ok": True, "path": str(path), "servers": sorted(servers)}
        if client == "claude":
            report["note"] = (
                "Claude Desktop: quit it completely (Quit, not closing the window) before "
                "`--install`, then start it. It writes its configuration back when it quits, "
                "so an edit made while it runs is lost and the old server keeps answering."
            )
        return report
    if client == "codex":
        path = codex_config_path()
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        for name, config in servers.items():
            content = _upsert_codex_server(content, name, config)
        atomic_write_bytes(path, (content).encode("utf-8"), mode=0o600)
        return {"ok": True, "path": str(path), "servers": sorted(servers)}
    if client == "claude-code":
        claude = shutil.which("claude")
        if not claude:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Claude Code's `claude` command is not on PATH. Install Claude Code, "
                "or run the previewed `claude mcp add-json` commands yourself.",
            )
        registered: list[str] = []
        for name, add in zip(servers, _claude_code_add(claude, servers), strict=True):
            added = subprocess.run(add, capture_output=True, text=True, check=False)
            if added.returncode and "already exists" in added.stderr + added.stdout:
                # Replace only a server Claude Code says exists, as the file clients do.
                remove = [claude, "mcp", "remove", "--scope", "user", name]
                subprocess.run(remove, capture_output=True, check=False)
                added = subprocess.run(add, capture_output=True, text=True, check=False)
            if added.returncode:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"`claude mcp add-json` failed for {name}: "
                    f"{(added.stderr or added.stdout).strip()} "
                    f"(already registered: {', '.join(registered) or 'none'})",
                    details={
                        "server": name,
                        "returncode": added.returncode,
                        "registered": registered,
                    },
                )
            registered.append(name)
        return {"ok": True, "path": str(claude_code_config_path()), "servers": sorted(servers)}
    raise SemanticLayerError("INVALID_CONFIG", f"Unsupported MCP client '{client}'")


def _client_servers(
    ref: PackageReference, *, selected_mcp: list[str], workspace_root: Path
) -> dict[str, dict[str, Any]]:
    servers: dict[str, dict[str, Any]] = {}
    if "query" in selected_mcp:
        command, *args = _launcher("semantic-rails", "semantic_rails.cli")
        servers["semantic-rails"] = {
            "command": command,
            "args": [*args, "mcp", "stdio", *(_ref_args(ref))],
        }
    if "architect" in selected_mcp:
        command, *args = _launcher("semantic-rails-architect-mcp", "semantic_rails.architect_mcp")
        servers["semantic-rails-architect"] = {
            "command": command,
            "args": [*args, "--transport", "stdio", "--workspace-root", str(workspace_root)],
        }
    return servers


def _launcher(script: str, module: str) -> list[str]:
    """The command a client config keeps using to start ``script`` from this install.

    ``uvx`` runs this interpreter from uv's cache (a directory above the environment
    holds a CACHEDIR.TAG), and ``uv cache prune`` deletes it. A config naming it would
    stop working, so name uv (``$UV``, which uv sets for what it runs) and a
    requirement that recreates this install instead.
    """

    uv = os.environ.get("UV")
    prefix = Path(sys.prefix).resolve()
    if not uv or not any((parent / "CACHEDIR.TAG").is_file() for parent in prefix.parents):
        return [sys.executable, "-m", module]
    dist = importlib.metadata.distribution("semantic-rails")
    present = {
        _canonical(name)
        for d in importlib.metadata.distributions()
        for name in d.metadata.get_all("Name") or []
    }
    return [uv, "tool", "run", "--from", _requirement(dist, present), script]


def _requirement(dist: importlib.metadata.Distribution, present: set[str]) -> str:
    """``dist`` as a requirement: the extras whose packages are all ``present``, then the
    source it came from (PEP 610 ``direct_url.json``) or its exact version."""

    extras = []
    for extra in dist.metadata.get_all("Provides-Extra") or []:
        marker = f'extra == "{extra}"'
        needs = {_canonical(re.split(r"[^\w.-]", r)[0]) for r in dist.requires or [] if marker in r}
        if needs and needs <= present:
            extras.append(extra)
    name = f"semantic-rails[{','.join(extras)}]" if extras else "semantic-rails"
    origin = json.loads(dist.read_text("direct_url.json") or "{}")
    if vcs := origin.get("vcs_info"):
        return f"{name} @ {vcs['vcs']}+{origin['url']}@{vcs['commit_id']}"
    return f"{name} @ {origin['url']}" if origin.get("url") else f"{name}=={dist.version}"


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _ref_args(ref: PackageReference) -> list[str]:
    if ref.package_id:
        return ["--package", ref.package_id]
    return ["--path", ref.source_path]


def _selected(value: str, allowed: tuple[str, ...], *, field: str) -> list[str]:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Unsupported {field} '{value}'",
            details={field: value, "allowed": list(allowed)},
        )
    if normalized == "both":
        return list(allowed[:2])
    return [normalized]


def _workspace_root_for_ref(ref: PackageReference) -> str:
    if ref.source_path:
        return str(Path(package_root_for_source(ref.source_path)).resolve().parent)
    return str(Path.cwd().resolve())


def _codex_section_name(name: str) -> str:
    return str(name).replace("-", "_")


def _codex_toml_block(name: str, config: dict[str, Any]) -> str:
    section = _codex_section_name(name)
    args = ", ".join(json.dumps(str(arg)) for arg in list(config.get("args", []) or []))
    lines = [
        f"[mcp_servers.{section}]",
        f"command = {json.dumps(str(config.get('command', '')))}",
        f"args = [{args}]",
        "enabled = true",
        "startup_timeout_sec = 30",
        "tool_timeout_sec = 30",
    ]
    return "\n".join(lines) + "\n"


def _upsert_codex_server(content: str, name: str, config: dict[str, Any]) -> str:
    section = _codex_section_name(name)
    pattern = re.compile(rf"(?ms)^\[mcp_servers\.{re.escape(section)}\]\n.*?(?=^\[|\Z)")
    block = _codex_toml_block(name, config)
    stripped = pattern.sub("", content).rstrip()
    return (stripped + "\n\n" if stripped else "") + block
