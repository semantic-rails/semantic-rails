from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path

import pytest

from semantic_rails.config_validation import PackageReference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp_manager import (
    available_mcp_servers,
    load_mcp_registry,
    mcp_status_report,
    save_mcp_registry,
    start_mcp_http_server,
    stop_mcp_http_server,
)


def test_stop_mcp_http_server_can_stop_by_package_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SEMANTIC_RAILS_HOME", str(tmp_path / "home"))
    package = tmp_path / "my_package"
    other_package = tmp_path / "other_package"
    package.mkdir()
    other_package.mkdir()
    save_mcp_registry(
        {
            "version": 1,
            "servers": {
                "default": {
                    "pid": 0,
                    "name": "default",
                    "transport": "http",
                    "package_path": str(package),
                },
                "other": {
                    "pid": 0,
                    "name": "other",
                    "transport": "http",
                    "package_path": str(other_package),
                },
            },
        }
    )

    report = stop_mcp_http_server(ref=PackageReference(source_path=str(package)))

    assert report["ok"] is True
    assert report["status"] == "not_running"
    assert [server["name"] for server in report["servers"]] == ["default"]
    registry = load_mcp_registry()
    assert sorted(registry["servers"]) == ["other"]


def test_stop_refuses_to_signal_a_reused_pid(tmp_path: Path, monkeypatch) -> None:
    import semantic_rails.mcp_manager as manager

    monkeypatch.setenv("SEMANTIC_RAILS_HOME", str(tmp_path / "home"))
    save_mcp_registry(
        {
            "version": 1,
            "servers": {
                "stale": {
                    "pid": os.getpid(),
                    "name": "stale",
                    "process_identity": {"started": "old", "command": "old"},
                }
            },
        }
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(manager, "_record_process_matches", lambda _record: False)
    monkeypatch.setattr(manager.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    report = stop_mcp_http_server("stale")

    assert report["status"] == "identity_mismatch"
    assert signals == []
    assert load_mcp_registry()["servers"] == {}


def test_start_fails_before_spawn_when_process_identity_is_unsupported(
    tmp_path: Path, monkeypatch
) -> None:
    import semantic_rails.mcp_manager as manager

    monkeypatch.setenv("SEMANTIC_RAILS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(manager.shutil, "which", lambda command: None if command == "ps" else "x")
    spawned = []
    monkeypatch.setattr(manager.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    with pytest.raises(SemanticLayerError, match="No child process|requires POSIX") as exc:
        start_mcp_http_server(PackageReference(source_path="", package_id="jaffle_shop"))

    assert exc.value.code == "UNSUPPORTED_PLATFORM"
    assert spawned == []


def test_cursor_and_claude_code_targets_install_and_both_stays_desktop_and_codex(
    tmp_path: Path, monkeypatch
) -> None:
    import json
    import subprocess

    import semantic_rails.mcp_manager as manager

    cursor = tmp_path / "cursor" / "mcp.json"
    cursor.parent.mkdir()
    cursor.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}))
    monkeypatch.setenv("SEMANTIC_RAILS_CURSOR_CONFIG", str(cursor))
    calls: list[list[str]] = []
    registered: set[str] = set()

    def claude(args: list[str], **_: object) -> subprocess.CompletedProcess:
        calls.append(args)
        verb, name = args[2], args[5]
        if verb == "add-json" and name in registered:
            return subprocess.CompletedProcess(args, 1, "", f"MCP server {name} already exists")
        (registered.add if verb == "add-json" else registered.discard)(name)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(manager.subprocess, "run", claude)
    ref = PackageReference(source_path="", package_id="jaffle_shop")

    both = manager.mcp_client_config_report(ref, client="both", workspace_root=str(tmp_path))
    assert sorted(both["previews"]) == ["claude", "codex"]
    for client in ("cursor", "claude-code", "claude-code", "cursor"):  # repeats replace
        report = manager.mcp_client_config_report(
            ref, client=client, workspace_root=str(tmp_path), install=True
        )
        assert report["installed"][client]["servers"] == [
            "semantic-rails",
            "semantic-rails-architect",
        ]

    servers = report["servers"]
    written = json.loads(cursor.read_text())
    assert written == {"keep": 1, "mcpServers": {"other": {"command": "x"}, **servers}}
    adds = {
        name: ["/bin/claude", "mcp", "add-json", "--scope", "user", name, json.dumps(config)]
        for name, config in servers.items()
    }
    replace = [
        step
        for name in servers
        for step in (
            adds[name],
            ["/bin/claude", "mcp", "remove", "--scope", "user", name],
            adds[name],
        )
    ]
    assert calls == list(adds.values()) + replace
    assert registered == set(servers)
    preview = manager.mcp_client_config_report(ref, client="claude-code", mcp="query")
    assert preview["previews"]["claude-code"]["commands"] == [
        f"claude mcp add-json --scope user semantic-rails '{json.dumps(servers['semantic-rails'])}'"
    ]


@pytest.mark.parametrize(
    ("claude", "error"),
    [(None, "not on PATH"), ("/bin/claude", "add-json` failed for semantic-rails: boom")],
)
def test_claude_code_install_reports_a_missing_or_failing_cli(
    tmp_path: Path, monkeypatch, claude: str | None, error: str
) -> None:
    import subprocess

    import semantic_rails.mcp_manager as manager

    calls: list[list[str]] = []
    monkeypatch.setattr(manager.shutil, "which", lambda _name: claude)
    monkeypatch.setattr(
        manager.subprocess,
        "run",
        lambda args, **_: calls.append(args) or subprocess.CompletedProcess(args, 1, "", "boom"),
    )

    with pytest.raises(SemanticLayerError, match=error):
        manager.mcp_client_config_report(
            PackageReference(source_path="", package_id="jaffle_shop"),
            client="claude-code",
            mcp="query",
            workspace_root=str(tmp_path),
            install=True,
        )
    assert not any("remove" in call for call in calls)  # an unrelated failure keeps the old server


@pytest.mark.parametrize("conflict", ["package", "host", "port", "health"])
def test_start_rejects_named_server_config_or_health_conflict_without_restart(
    tmp_path: Path, monkeypatch, conflict: str
) -> None:
    import semantic_rails.mcp_manager as manager

    monkeypatch.setenv("SEMANTIC_RAILS_HOME", str(tmp_path / "home"))
    record = {
        "pid": 4242,
        "name": "named",
        "transport": "http",
        "host": "127.0.0.1",
        "port": 8091,
        "package_id": "jaffle_shop",
        "package_path": "",
        "process_identity": {"started": "now", "command": "semantic-rails mcp http"},
        "instance_nonce": "registered-nonce",
    }
    save_mcp_registry({"version": 1, "servers": {"named": record}})
    monkeypatch.setattr(
        manager,
        "managed_mcp_lifecycle_report",
        lambda: {"supported": True},
    )
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(manager, "_record_process_matches", lambda _record: True)
    monkeypatch.setattr(
        manager,
        "_http_health",
        lambda *a, **k: {
            "ok": conflict != "health",
            "status": 200,
            "payload": {
                "service": "semantic-rails-mcp",
                "package_id": "jaffle_shop",
                "instance_nonce": "registered-nonce",
            },
        },
    )
    spawned = []
    monkeypatch.setattr(manager.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    ref = PackageReference(
        source_path="",
        package_id="other_package" if conflict == "package" else "jaffle_shop",
    )
    host = "0.0.0.0" if conflict == "host" else "127.0.0.1"
    port = 8092 if conflict == "port" else 8091
    with pytest.raises(SemanticLayerError) as exc:
        start_mcp_http_server(ref, name="named", host=host, port=port)

    assert exc.value.code == "CONFIG_CONFLICT"
    assert exc.value.details["restart_performed"] is False
    expected_mismatch = "package_id" if conflict == "package" else conflict
    assert expected_mismatch in exc.value.details["mismatches"]
    assert spawned == []
    assert load_mcp_registry()["servers"]["named"] == record


def test_start_returns_already_running_only_for_exact_healthy_named_server(
    tmp_path: Path, monkeypatch
) -> None:
    import semantic_rails.mcp_manager as manager

    monkeypatch.setenv("SEMANTIC_RAILS_HOME", str(tmp_path / "home"))
    record = {
        "pid": 4242,
        "name": "named",
        "transport": "http",
        "host": "127.0.0.1",
        "port": 8091,
        "package_id": "jaffle_shop",
        "package_path": "",
        "process_identity": {"started": "now", "command": "semantic-rails mcp http"},
        "instance_nonce": "registered-nonce",
    }
    save_mcp_registry({"version": 1, "servers": {"named": record}})
    monkeypatch.setattr(
        manager,
        "managed_mcp_lifecycle_report",
        lambda: {"supported": True},
    )
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(manager, "_record_process_matches", lambda _record: True)
    monkeypatch.setattr(
        manager,
        "_http_health",
        lambda *a, **k: {
            "ok": True,
            "status": 200,
            "payload": {
                "service": "semantic-rails-mcp",
                "package_id": "jaffle_shop",
                "instance_nonce": "registered-nonce",
            },
        },
    )
    spawned = []
    monkeypatch.setattr(manager.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    report = start_mcp_http_server(
        PackageReference(source_path="", package_id="jaffle_shop"),
        name="named",
    )

    assert report["status"] == "already_running"
    assert report["server"]["process_identity_verified"] is True
    assert report["server"]["health"]["ok"] is True
    assert spawned == []


def test_available_servers_uses_foreground_http_when_managed_lifecycle_is_unsupported(
    monkeypatch,
) -> None:
    import semantic_rails.mcp_manager as manager

    monkeypatch.setattr(
        manager,
        "managed_mcp_lifecycle_report",
        lambda: {
            "supported": False,
            "platform": "win32",
            "mode": "foreground-only",
            "reason": "Managed lifecycle is unavailable.",
            "windows_alternative": "Use stdio or foreground HTTP.",
        },
    )

    rows = available_mcp_servers(PackageReference(source_path="", package_id="jaffle_shop"))
    http = next(row for row in rows if row["name"] == "semantic-rails-query-http")

    assert http["command"] == [
        "semantic-rails",
        "mcp",
        "http",
        "--package",
        "jaffle_shop",
    ]
    assert http["managed_by_start_stop"] is False
    assert http["foreground"] is True


@pytest.mark.skipif(shutil.which("ps") is None, reason="process identity requires ps")
def test_managed_mcp_server_lifecycle_waits_for_health_and_verifies_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SEMANTIC_RAILS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SEMANTIC_RAILS_MCP_START_TIMEOUT_SECONDS", "10")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    started = start_mcp_http_server(
        PackageReference(source_path="", package_id="jaffle_shop"),
        name="lifecycle",
        port=port,
    )
    try:
        assert started["ok"] is True, started
        assert started["status"] == "started"
        assert started["server"]["health"]["ok"] is True
        assert started["server"]["process_identity_verified"] is True
        nonce = started["server"]["health"]["payload"]["instance_nonce"]
        assert nonce

        collision = start_mcp_http_server(
            PackageReference(source_path="", package_id="jaffle_shop"),
            name="collision",
            port=port,
        )
        assert collision["ok"] is False
        assert collision["status"] == "failed_to_start"
        assert "collision" not in load_mcp_registry()["servers"]

        status = mcp_status_report()
        row = next(item for item in status["servers"] if item["name"] == "lifecycle")
        assert row["process_alive"] is True
        assert row["process_identity_verified"] is True
    finally:
        stopped = stop_mcp_http_server("lifecycle")

    assert stopped["status"] == "stopped"
    assert load_mcp_registry()["servers"] == {}
