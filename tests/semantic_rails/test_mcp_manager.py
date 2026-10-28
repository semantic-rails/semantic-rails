from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from semantic_rails.config_validation import PackageReference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp_manager import _requirement, mcp_client_config_report


@pytest.mark.parametrize("client", ["claude", "codex", "cursor", "claude-code", "both"])
def test_only_claude_desktop_install_reports_and_prints_quit_note(
    tmp_path: Path, monkeypatch, capsys, client: str
) -> None:
    import semantic_rails.mcp_manager as manager
    from semantic_rails.cli.commands.mcp import _print_mcp_setup_report

    for name in ("CLAUDE", "CODEX", "CURSOR"):
        monkeypatch.setenv(f"SEMANTIC_RAILS_{name}_CONFIG", str(tmp_path / name))
    monkeypatch.setattr(manager.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        manager.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    report = mcp_client_config_report(
        PackageReference(source_path="", package_id="jaffle_shop"),
        client=client,
        workspace_root=str(tmp_path),
        install=True,
    )
    note = (
        "Claude Desktop: quit it completely (Quit, not closing the window) before "
        "`--install`, then start it. It writes its configuration back when it quits, "
        "so an edit made while it runs is lost and the old server keeps answering."
    )
    for installed_client, result in report["installed"].items():
        if installed_client == "claude":
            assert result["note"] == note
        else:
            assert "note" not in result
    _print_mcp_setup_report({"ok": True, "mode": "install", "client_config": report})
    output = capsys.readouterr().out
    if client in ("claude", "both"):
        assert output.index(note) > output.index("Installed:")
    else:
        assert note not in output


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
    ("claude", "mcp", "error"),
    [
        (None, "query", "not on PATH"),
        ("/bin/claude", "query", r"for semantic-rails: boom \(already registered: none\)"),
        (
            "/bin/claude",
            "both",  # the query server is added, then Architect fails
            r"for semantic-rails-architect: boom \(already registered: semantic-rails\)",
        ),
    ],
)
def test_claude_code_install_reports_a_missing_or_failing_cli(
    tmp_path: Path, monkeypatch, claude: str | None, mcp: str, error: str
) -> None:
    import subprocess

    import semantic_rails.mcp_manager as manager

    calls: list[list[str]] = []
    failing = "semantic-rails-architect" if mcp == "both" else "semantic-rails"

    def run(args: list[str], **_: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return subprocess.CompletedProcess(args, int(args[5] == failing), "", "boom")

    monkeypatch.setattr(manager.shutil, "which", lambda _name: claude)
    monkeypatch.setattr(manager.subprocess, "run", run)

    with pytest.raises(SemanticLayerError, match=error):
        manager.mcp_client_config_report(
            PackageReference(source_path="", package_id="jaffle_shop"),
            client="claude-code",
            mcp=mcp,
            workspace_root=str(tmp_path),
            install=True,
        )
    assert not any("remove" in call for call in calls)  # an unrelated failure keeps the old server


STAT = b"42 (python) S " + b" ".join(str(field).encode() for field in range(4, 53))


@pytest.mark.parametrize(
    ("in_cache", "uv", "via_uv"),
    [(True, "/opt/uv/bin/uv", True), (False, "/opt/uv/bin/uv", False), (True, None, False)],
)
def test_client_config_outlives_a_pruned_uv_cache(
    tmp_path: Path, monkeypatch, in_cache: bool, uv: str | None, via_uv: bool
) -> None:
    cache = tmp_path / "uv-cache"
    env = cache / "archive-v0" / "abc123"
    (env / "bin").mkdir(parents=True)
    (env / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    if in_cache:
        (cache / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    monkeypatch.setattr(sys, "prefix", str(env))
    monkeypatch.setattr(sys, "executable", str(env / "bin" / "python"))
    if uv:
        monkeypatch.setenv("UV", uv)
    else:
        monkeypatch.delenv("UV", raising=False)
    ref = PackageReference(source_path="", package_id="jaffle_shop")

    servers = mcp_client_config_report(ref, client="claude", workspace_root=str(tmp_path))
    shutil.rmtree(cache)  # what `uv cache prune` does to a uvx environment

    commands = [[row["command"], *row["args"]] for row in servers["servers"].values()]
    if via_uv:
        for command in commands:
            assert command[:4] == ["/opt/uv/bin/uv", "tool", "run", "--from"]
            assert command[4].startswith("semantic-rails")
            assert str(env) not in json.dumps(command)
    else:
        assert all(command[:2] == [str(env / "bin" / "python"), "-m"] for command in commands)
    query = servers["servers"]["semantic-rails"]["args"]
    assert query[-4:] == ["mcp", "stdio", "--package", "jaffle_shop"]


@pytest.mark.parametrize(
    ("present", "direct_url", "expected"),
    [
        (set(), None, "semantic-rails==0.3.0"),
        (
            {"adbc-driver-manager", "adbc-driver-postgresql", "pyarrow"},
            None,
            "semantic-rails[postgres]==0.3.0",
        ),
        (
            {"adbc-driver-manager", "adbc-driver-postgresql", "pyarrow", "pyathena"},
            None,
            "semantic-rails[postgres,all]==0.3.0",
        ),
        (set(), {"url": "file:///src", "dir_info": {}}, "semantic-rails @ file:///src"),
        (
            {"adbc-driver-manager", "adbc-driver-postgresql", "pyarrow"},
            {"url": "https://example.com/r.git", "vcs_info": {"vcs": "git", "commit_id": "c0"}},
            "semantic-rails[postgres] @ git+https://example.com/r.git@c0",
        ),
    ],
)
def test_requirement_recreates_extras_version_and_source(
    tmp_path: Path, present: set[str], direct_url: dict | None, expected: str
) -> None:
    info = tmp_path / "semantic_rails-0.3.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: semantic-rails\nVersion: 0.3.0\n"
        "Requires-Dist: duckdb>=1.5.3\n"
        "Provides-Extra: postgres\n"
        'Requires-Dist: adbc-driver-postgresql==1.12.0; extra == "postgres"\n'
        'Requires-Dist: adbc-driver-manager==1.12.0; extra == "postgres"\n'
        'Requires-Dist: pyarrow>=25.0.1; extra == "postgres"\n'
        "Provides-Extra: all\n"
        'Requires-Dist: adbc-driver-postgresql==1.12.0; extra == "all"\n'
        'Requires-Dist: adbc-driver-manager==1.12.0; extra == "all"\n'
        'Requires-Dist: pyarrow>=25.0.1; extra == "all"\n'
        'Requires-Dist: pyathena>=3.32.0; extra == "all"\n'
    )
    if direct_url:
        (info / "direct_url.json").write_text(json.dumps(direct_url))

    assert _requirement(importlib.metadata.PathDistribution(info), present) == expected
