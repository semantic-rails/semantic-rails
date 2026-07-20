"""Local MCP process and client-configuration helpers.

These helpers are deliberately local-developer conveniences. They do not
change the MCP protocol server, hosted deployment behavior, or package format.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from .config import load_package_config, package_root_for_source
from .config_validation import PackageReference
from .errors import SemanticLayerError
from .local_config import semantic_rails_home

DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8091
CLIENTS = ("claude", "codex", "both")
MCP_KINDS = ("query", "architect", "both")


def managed_mcp_lifecycle_report() -> dict[str, Any]:
    """Describe whether safe background start/stop is available here."""

    supported = bool(
        os.name == "posix" and shutil.which("ps") is not None and _process_identity(os.getpid())
    )
    if supported:
        reason = "POSIX process identity and signaling are available."
    else:
        reason = (
            "Managed background MCP start/stop requires POSIX process identity. "
            "Use generated stdio client config, or run `semantic-rails mcp http` "
            "in a foreground terminal."
        )
    return {
        "supported": supported,
        "platform": sys.platform,
        "mode": "managed-background" if supported else "foreground-only",
        "reason": reason,
        "windows_alternative": (
            "Run `semantic-rails mcp setup --install --yes` so Claude/Codex launches stdio, "
            "or keep `semantic-rails mcp http` open in a terminal."
        ),
    }


def mcp_state_dir() -> Path:
    return semantic_rails_home() / "mcp"


def mcp_registry_path() -> Path:
    return mcp_state_dir() / "servers.yml"


def mcp_logs_dir() -> Path:
    return mcp_state_dir() / "logs"


@contextlib.contextmanager
def _mcp_registry_lock(registry_path: Path):
    """Serialize registry read/modify/write sequences across CLI processes."""

    lock_path = registry_path.with_name(f".{registry_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "posix":
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            locking = msvcrt.locking  # type: ignore[attr-defined]
            lock_mode = int(msvcrt.LK_LOCK)  # type: ignore[attr-defined]
            unlock_mode = int(msvcrt.LK_UNLCK)  # type: ignore[attr-defined]
            locking(handle.fileno(), lock_mode, 1)
            try:
                yield
            finally:
                handle.seek(0)
                locking(handle.fileno(), unlock_mode, 1)
            return
        raise SemanticLayerError(
            "UNSUPPORTED_PLATFORM",
            f"MCP registry locking is not supported on platform {sys.platform!r}.",
        )


def load_mcp_registry(path: str | Path | None = None) -> dict[str, Any]:
    registry_path = Path(path).expanduser() if path else mcp_registry_path()
    with _mcp_registry_lock(registry_path):
        return _load_mcp_registry_unlocked(registry_path)


def _load_mcp_registry_unlocked(registry_path: Path) -> dict[str, Any]:
    if not registry_path.exists():
        return {"version": 1, "servers": {}}
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"MCP registry must be a YAML mapping: {registry_path}",
            details={"path": str(registry_path)},
        )
    servers = raw.get("servers", {}) or {}
    if not isinstance(servers, dict):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"MCP registry servers must be a YAML mapping: {registry_path}",
            details={"path": str(registry_path)},
        )
    return {"version": int(raw.get("version", 1) or 1), "servers": dict(servers)}


def save_mcp_registry(registry: dict[str, Any]) -> Path:
    path = mcp_registry_path()
    with _mcp_registry_lock(path):
        _save_mcp_registry_unlocked(registry, path)
    return path


def _save_mcp_registry_unlocked(registry: dict[str, Any], path: Path) -> None:
    _atomic_write_text(
        path,
        yaml.safe_dump(registry, sort_keys=False, allow_unicode=False),
    )


def start_mcp_http_server(
    ref: PackageReference,
    *,
    name: str = "default",
    host: str = DEFAULT_MCP_HOST,
    port: int = DEFAULT_MCP_PORT,
) -> dict[str, Any]:
    _assert_process_identity_supported()
    server_name = _server_name(name)
    registry_path = mcp_registry_path()
    expected_package_id = ref.package_id or str(
        load_package_config(ref.source_path).package.package_id
    )
    requested_config = {
        "package_id": expected_package_id,
        "package_path": _normalized_package_path(ref.source_path),
        "host": host,
        "port": int(port),
    }
    with _mcp_registry_lock(registry_path):
        registry = _load_mcp_registry_unlocked(registry_path)
        existing = dict(registry["servers"].get(server_name, {}) or {})
        if existing:
            registered_config = {
                "package_id": str(existing.get("package_id", "") or ""),
                "package_path": _normalized_package_path(
                    str(existing.get("package_path", "") or "")
                ),
                "host": str(existing.get("host", "") or ""),
                "port": int(existing.get("port", 0) or 0),
            }
            mismatches = [
                key
                for key, value in requested_config.items()
                if registered_config.get(key) != value
            ]
            row = _status_row(server_name, existing)
            if not row["process_identity_verified"]:
                mismatches.append("process_identity")
            if not str(existing.get("instance_nonce", "") or ""):
                mismatches.append("instance_nonce")
            if not dict(row.get("health", {}) or {}).get("ok"):
                mismatches.append("health")
            if not mismatches:
                return {
                    "ok": True,
                    "status": "already_running",
                    "server": row,
                    "registry_path": str(registry_path),
                }
            raise SemanticLayerError(
                "CONFIG_CONFLICT",
                f"Managed MCP server name '{server_name}' is already registered with "
                "a different or unhealthy configuration. Stop it explicitly before "
                "starting a replacement; no process was restarted.",
                details={
                    "name": server_name,
                    "requested": requested_config,
                    "registered": registered_config,
                    "mismatches": sorted(set(mismatches)),
                    "server": row,
                    "restart_performed": False,
                    "registry_path": str(registry_path),
                },
            )

        instance_nonce = secrets.token_urlsafe(24)
        mcp_logs_dir().mkdir(parents=True, exist_ok=True)
        log_path = mcp_logs_dir() / f"{server_name}.log"
        cmd = [
            sys.executable,
            "-m",
            "semantic_rails.cli",
            "mcp",
            "http",
            "--host",
            host,
            "--port",
            str(port),
        ]
        if ref.package_id:
            cmd.extend(["--package", ref.package_id])
        else:
            cmd.extend(["--path", ref.source_path])
        env = dict(os.environ)
        env.setdefault("SEMANTIC_RAILS_HOME", str(semantic_rails_home()))
        env["SEMANTIC_RAILS_MCP_INSTANCE_NONCE"] = instance_nonce
        with log_path.open("ab") as log:
            proc = subprocess.Popen(  # noqa: S603 - fixed interpreter/module command.
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        process_identity = _process_identity(proc.pid)
        record = {
            "pid": proc.pid,
            "name": server_name,
            "transport": "http",
            "host": host,
            "port": port,
            "package_id": expected_package_id,
            "package_path": requested_config["package_path"],
            "command": cmd,
            "log_path": str(log_path),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "process_identity": process_identity,
            "instance_nonce": instance_nonce,
        }
        health = _wait_for_http_health(
            proc,
            host=host,
            port=port,
            expected_nonce=instance_nonce,
            expected_package_id=expected_package_id,
        )
        if not health.get("ok") or not _record_process_matches(record):
            _terminate_spawned_process(proc)
            row = _status_row(server_name, record)
            row["health"] = health
            return {
                "ok": False,
                "status": "failed_to_start",
                "server": row,
                "registry_path": str(registry_path),
            }
        registry["servers"].pop(server_name, None)
        registry["servers"][server_name] = record
        _save_mcp_registry_unlocked(registry, registry_path)
        row = _status_row(server_name, record, health=health)
        return {
            "ok": bool(row["process_alive"] and health.get("ok")),
            "status": (
                "started" if row["process_alive"] and health.get("ok") else "failed_to_start"
            ),
            "server": row,
            "registry_path": str(registry_path),
        }


def stop_mcp_http_server(
    name: str = "default", ref: PackageReference | None = None
) -> dict[str, Any]:
    registry_path = mcp_registry_path()
    with _mcp_registry_lock(registry_path):
        registry = _load_mcp_registry_unlocked(registry_path)
        if ref is not None:
            return _stop_mcp_http_servers_for_ref(ref, registry, registry_path)
        server_name = _server_name(name)
        record = dict(registry["servers"].get(server_name, {}) or {})
        if not record:
            return {
                "ok": True,
                "status": "not_registered",
                "name": server_name,
                "registry_path": str(registry_path),
            }
        stopped_row = _stop_recorded_process(record)
        pid = int(record.get("pid", 0) or 0)
        registry["servers"].pop(server_name, None)
        _save_mcp_registry_unlocked(registry, registry_path)
        return {
            "ok": True,
            "status": stopped_row["status"],
            "name": server_name,
            "pid": pid,
            "registry_path": str(registry_path),
        }


def _stop_mcp_http_servers_for_ref(
    ref: PackageReference, registry: dict[str, Any], registry_path: Path
) -> dict[str, Any]:
    matches = [
        (name, dict(record or {}))
        for name, record in sorted(dict(registry.get("servers", {}) or {}).items())
        if _server_record_matches_ref(dict(record or {}), ref)
    ]
    if not matches:
        return {
            "ok": True,
            "status": "not_registered",
            "package": {"id": ref.package_id, "source_path": ref.source_path},
            "registry_path": str(registry_path),
        }

    stopped = []
    for server_name, record in matches:
        stopped.append(_stop_registered_mcp_server(registry, server_name, record))
    _save_mcp_registry_unlocked(registry, registry_path)
    return {
        "ok": True,
        "status": "stopped"
        if any(row["status"] == "stopped" for row in stopped)
        else "not_running",
        "package": {"id": ref.package_id, "source_path": ref.source_path},
        "servers": stopped,
        "registry_path": str(registry_path),
    }


def _stop_registered_mcp_server(
    registry: dict[str, Any], server_name: str, record: dict[str, Any]
) -> dict[str, Any]:
    pid = int(record.get("pid", 0) or 0)
    stopped_row = _stop_recorded_process(record)
    registry["servers"].pop(server_name, None)
    return {
        "name": server_name,
        "pid": pid,
        "status": stopped_row["status"],
    }


def mcp_status_report(ref: PackageReference | None = None) -> dict[str, Any]:
    registry = load_mcp_registry()
    servers = [
        _status_row(name, dict(record or {}))
        for name, record in sorted(dict(registry.get("servers", {}) or {}).items())
    ]
    return {
        "ok": True,
        "registry_path": str(mcp_registry_path()),
        "servers": servers,
        "available": available_mcp_servers(ref),
    }


def available_mcp_servers(ref: PackageReference | None = None) -> list[dict[str, Any]]:
    package_arg = []
    package_label = ""
    if ref is not None:
        if ref.package_id:
            package_arg = ["--package", ref.package_id]
            package_label = ref.package_id
        else:
            package_arg = ["--path", ref.source_path]
            package_label = ref.source_path
    lifecycle = managed_mcp_lifecycle_report()
    http_command = (
        ["semantic-rails", "mcp", "start", *package_arg]
        if lifecycle["supported"]
        else ["semantic-rails", "mcp", "http", *package_arg]
    )
    return [
        {
            "name": "semantic-rails-query-stdio",
            "kind": "query",
            "transport": "stdio",
            "package": package_label,
            "command": ["semantic-rails", "mcp", "stdio", *package_arg],
            "managed_by_start_stop": False,
        },
        {
            "name": "semantic-rails-query-http",
            "kind": "query",
            "transport": "http",
            "package": package_label,
            "command": http_command,
            "managed_by_start_stop": bool(lifecycle["supported"]),
            "foreground": not bool(lifecycle["supported"]),
            "lifecycle": lifecycle,
        },
        {
            "name": "semantic-rails-architect-stdio",
            "kind": "architect",
            "transport": "stdio",
            "command": ["semantic-rails-architect-mcp", "--transport", "stdio"],
            "managed_by_start_stop": False,
        },
    ]


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


def _client_preview(client: str, servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if client == "claude":
        return {"path": str(claude_config_path()), "mcpServers": servers}
    if client == "codex":
        blocks = [_codex_toml_block(name, config) for name, config in servers.items()]
        return {"path": str(codex_config_path()), "toml": "\n".join(blocks).strip() + "\n"}
    raise SemanticLayerError("INVALID_CONFIG", f"Unsupported MCP client '{client}'")


def _install_client_config(client: str, servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if client == "claude":
        path = claude_config_path()
        data: dict[str, Any] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            if not isinstance(data, dict):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"Claude config must be a JSON object: {path}",
                    details={"path": str(path)},
                )
        mcp_servers = dict(data.get("mcpServers", {}) or {})
        mcp_servers.update(servers)
        data["mcpServers"] = mcp_servers
        _atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
        return {"ok": True, "path": str(path), "servers": sorted(servers)}
    if client == "codex":
        path = codex_config_path()
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        for name, config in servers.items():
            content = _upsert_codex_server(content, name, config)
        _atomic_write_text(path, content)
        return {"ok": True, "path": str(path), "servers": sorted(servers)}
    raise SemanticLayerError("INVALID_CONFIG", f"Unsupported MCP client '{client}'")


def _client_servers(
    ref: PackageReference, *, selected_mcp: list[str], workspace_root: Path
) -> dict[str, dict[str, Any]]:
    servers: dict[str, dict[str, Any]] = {}
    if "query" in selected_mcp:
        servers["semantic-rails"] = {
            "command": sys.executable,
            "args": [
                "-m",
                "semantic_rails.cli",
                "mcp",
                "stdio",
                *(_ref_args(ref)),
            ],
        }
    if "architect" in selected_mcp:
        servers["semantic-rails-architect"] = {
            "command": sys.executable,
            "args": [
                "-m",
                "semantic_rails.architect_mcp",
                "--transport",
                "stdio",
                "--workspace-root",
                str(workspace_root),
            ],
        }
    return servers


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
        return [item for item in allowed if item != "both"]
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


def _server_record_matches_ref(record: dict[str, Any], ref: PackageReference) -> bool:
    if ref.package_id and str(record.get("package_id", "")) == ref.package_id:
        return True
    if not ref.source_path:
        return False
    record_path = str(record.get("package_path", "") or "")
    if not record_path:
        return False
    return Path(record_path).expanduser().resolve() == Path(ref.source_path).expanduser().resolve()


def _normalized_package_path(value: str) -> str:
    """Canonicalize a package source for managed-server identity checks."""

    if not value:
        return ""
    return str(Path(value).expanduser().resolve())


def _status_row(
    name: str, record: dict[str, Any], *, health: dict[str, Any] | None = None
) -> dict[str, Any]:
    pid = int(record.get("pid", 0) or 0)
    pid_alive = bool(pid and _pid_alive(pid))
    identity_verified = bool(pid_alive and _record_process_matches(record))
    resolved_health = (
        health
        if health is not None
        else (
            _http_health(
                str(record.get("host", "")),
                int(record.get("port", 0) or 0),
                expected_nonce=str(record.get("instance_nonce", "") or ""),
                expected_package_id=str(record.get("package_id", "") or ""),
            )
            if identity_verified
            else {}
        )
    )
    return {
        "name": name,
        "transport": record.get("transport", "http"),
        "pid": pid,
        "process_alive": identity_verified,
        "pid_alive": pid_alive,
        "process_identity_verified": identity_verified,
        "health": resolved_health,
        "host": record.get("host", ""),
        "port": record.get("port", 0),
        "package_id": record.get("package_id", ""),
        "package_path": record.get("package_path", ""),
        "log_path": record.get("log_path", ""),
        "started_at": record.get("started_at", ""),
    }


def _http_health(
    host: str,
    port: int,
    *,
    expected_nonce: str = "",
    expected_package_id: str = "",
) -> dict[str, Any]:
    if not host or not port:
        return {}
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=0.75) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
            mismatches = []
            if payload.get("service") != "semantic-rails-mcp":
                mismatches.append("service")
            if expected_nonce and payload.get("instance_nonce") != expected_nonce:
                mismatches.append("instance_nonce")
            if expected_package_id and payload.get("package_id") != expected_package_id:
                mismatches.append("package_id")
            return {
                "ok": not mismatches,
                "status": response.status,
                "payload": payload,
                **(
                    {"error": f"health identity mismatch: {', '.join(mismatches)}"}
                    if mismatches
                    else {}
                ),
            }
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _assert_process_identity_supported() -> None:
    """Fail before spawning when safe PID-reuse protection is unavailable."""

    support = managed_mcp_lifecycle_report()
    if not support["supported"]:
        raise SemanticLayerError(
            "UNSUPPORTED_PLATFORM",
            f"{support['reason']} No child process was spawned.",
            details={**support, "spawned": False},
        )


def _process_identity(pid: int) -> dict[str, str]:
    """Return stable OS-observed identity fields for a live process.

    The registry never relies on PID alone: PIDs can be reused after a
    local MCP server exits, and signaling a reused PID could terminate an
    unrelated process. ``lstart`` is stable for the process lifetime and
    the command guards against a different executable occupying the PID.
    """

    if not _pid_alive(pid):
        return {}
    values: dict[str, str] = {}
    for key, field in (("started", "lstart="), ("command", "command=")):
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", field],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return {}
        value = result.stdout.strip() if result.returncode == 0 else ""
        if not value:
            return {}
        values[key] = value
    return values


def _record_process_matches(record: dict[str, Any]) -> bool:
    pid = int(record.get("pid", 0) or 0)
    expected = dict(record.get("process_identity", {}) or {})
    if not pid or not expected.get("started") or not expected.get("command"):
        return False
    return _process_identity(pid) == expected


def _wait_for_http_health(
    proc: subprocess.Popen[Any],
    *,
    host: str,
    port: int,
    expected_nonce: str,
    expected_package_id: str,
) -> dict[str, Any]:
    try:
        timeout = float(os.environ.get("SEMANTIC_RAILS_MCP_START_TIMEOUT_SECONDS", "8") or 8)
    except ValueError:
        timeout = 8.0
    deadline = time.monotonic() + max(0.25, timeout)
    last: dict[str, Any] = {"ok": False, "error": "server did not become healthy"}
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return {
                "ok": False,
                "error": f"server process exited with status {proc.returncode}",
            }
        last = _http_health(
            host,
            port,
            expected_nonce=expected_nonce,
            expected_package_id=expected_package_id,
        )
        if last.get("ok"):
            return last
        time.sleep(0.1)
    return last


def _terminate_spawned_process(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(OSError):
        proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()


def _stop_recorded_process(record: dict[str, Any]) -> dict[str, Any]:
    pid = int(record.get("pid", 0) or 0)
    if not pid or not _pid_alive(pid):
        return {"status": "not_running"}
    if not _record_process_matches(record):
        return {"status": "identity_mismatch"}
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return {"status": "stopped"}
        if not _record_process_matches(record):
            # The verified process exited. A zombie may still answer
            # ``kill(pid, 0)`` until it is reaped, and a rapidly reused PID
            # must never receive the follow-up SIGKILL.
            return {"status": "stopped"}
        time.sleep(0.1)
    if _record_process_matches(record):
        os.kill(pid, signal.SIGKILL)
        return {"status": "stopped"}
    return {"status": "stopped"}


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _server_name(name: str) -> str:
    text = str(name or "default").strip()
    if not text:
        text = "default"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text)


def executable_hint() -> str:
    return shutil.which("semantic-rails") or sys.executable
