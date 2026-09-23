"""How scripts/verify_readme_quickstart.py judges results; the README itself is checked in CI."""

from __future__ import annotations

import json
import re
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from scripts import verify_readme_quickstart as quickstart

README = f"... {quickstart.FALLBACK_WARNING} ... {quickstart.PYTHON_WARNING} ..."
NOTE = quickstart.NOTE


def is_failure(detail: str) -> bool:
    return bool(detail) and not detail.startswith(NOTE)


class Scripted(quickstart.Environment):
    """Answers each command with the next canned (returncode, stdout, stderr)."""

    name = "scripted"

    def __init__(self, root: Path, *results: tuple[int, str, str]) -> None:
        self.root = root
        self.results = list(results)
        self.commands: list[str] = []

    def run(self, command: str) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        returncode, stdout, stderr = self.results.pop(0)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def workdir(self) -> str:
        return str(self.root / "work")

    def scratch(self, name: str) -> str:
        return str(self.root / "scratch" / name)


def answer(package: str) -> str:
    return json.dumps({"package": {"id": package}, "result": {"rows": [{"n": 1}]}})


REFUSAL = json.dumps(
    {
        "ok": False,
        "status": "error",
        "error": {
            "code": "INVALID_CONFIG",
            "message": "No package selected. Choose one: ...",
            "details": {"reason": "no_package_selected"},
        },
    }
)
OTHER_ERROR = json.dumps(
    {"ok": False, "status": "error", "error": {"code": "INVALID_CONFIG", "details": {}}}
)


@pytest.mark.parametrize(
    ("result", "readme", "outcome"),
    [
        ((0, answer("jaffle_shop"), ""), README, "pass"),
        ((0, answer("jaffle_shop"), ""), "no warnings", "fail"),
        ((1, REFUSAL, ""), README, "note"),
        ((1, REFUSAL, ""), "no warnings", "pass"),
        # A broken run is never read as a fixed trap.
        ((1, "", "Traceback (most recent call last):\n  ...\nImportError: boom"), README, "fail"),
        ((0, "not json", ""), README, "fail"),
        ((0, "{}", ""), README, "fail"),
        ((0, answer("my_package"), ""), README, "fail"),
        ((1, OTHER_ERROR, ""), README, "fail"),
        ((0, REFUSAL, ""), README, "fail"),
    ],
    ids=[
        "falls back, warned",
        "falls back, unwarned",
        "refuses, warning left",
        "refuses, warning gone",
        "crash",
        "malformed json",
        "empty report",
        "another package",
        "another error",
        "refusal with exit 0",
    ],
)
def test_fallback_trap_only_accepts_the_fallback_or_the_documented_refusal(
    tmp_path: Path, result: tuple[int, str, str], readme: str, outcome: str
) -> None:
    detail = quickstart.fallback_trap(Scripted(tmp_path, result), readme)
    status = "note" if detail.startswith(NOTE) else ("fail" if detail else "pass")
    assert status == outcome, detail


@pytest.mark.parametrize(
    ("returncode", "stdout", "refusal"),
    [(1, REFUSAL, True), (0, REFUSAL, False), (1, OTHER_ERROR, False), (1, "not json", False)],
    ids=["refusal", "exit 0", "another error", "not json"],
)
def test_no_package_selected_needs_the_documented_refusal(
    tmp_path: Path, returncode: int, stdout: str, refusal: bool
) -> None:
    ask = quickstart.report(Scripted(tmp_path, (returncode, stdout, "")), quickstart.TRY)
    assert quickstart.no_package_selected(ask) is refusal


@pytest.mark.parametrize(
    ("result", "problem"),
    [
        ((0, answer("jaffle_shop"), ""), ""),
        ((1, "", "Traceback (most recent call last):\nImportError: boom"), "exit 1: Traceback"),
        ((0, "2 rows", ""), "printed no JSON report: 2 rows"),
        ((0, "[1, 2]", ""), "printed no JSON report"),
    ],
    ids=["report", "crash", "not json", "not an object"],
)
def test_report_keeps_what_went_wrong(
    tmp_path: Path, result: tuple[int, str, str], problem: str
) -> None:
    env = Scripted(tmp_path, result)
    ask = quickstart.report(env, quickstart.TRY)
    assert env.commands == [f"{quickstart.TRY} --json"]
    assert ask.problem().startswith(problem) and bool(ask.problem()) == bool(problem)


def fake_uv(directory: Path) -> Path:
    """A `uv` that makes a venv whose Python is 3.9.6, and whose pip refuses the package."""
    script = directory / "uv"
    script.write_text(
        textwrap.dedent(
            """\
            #!/bin/bash
            if [ "$1" = venv ]; then
              mkdir -p "$2/bin"
              printf '#!/bin/bash\\necho "Python 3.9.6"\\n' > "$2/bin/python"
              chmod +x "$2/bin/python"
              echo "$UV_PYTHON_INSTALL_DIR" > "$2/managed-python-dir"
              exit 0
            fi
            echo "error: semantic-rails requires Python>=3.11" >&2
            exit 1
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.mark.skipif(sys.platform == "win32", reason="the local environment runs /bin/bash")
def test_bare_venv_check_stays_inside_its_own_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uv = fake_uv(tmp_path)
    monkeypatch.setattr(quickstart.shutil, "which", lambda name: str(uv))
    # Directories outside the run, including the path the check once deleted.
    outside = tmp_path / "outside" / "bare-venv"
    outside.mkdir(parents=True)
    (outside / "keep").write_text("mine")

    first, second = quickstart.Local(), quickstart.Local()
    try:
        assert first.root != second.root
        assert Path(first.env["TMPDIR"]).is_relative_to(first.root)
        venv, other = first.scratch("bare-venv"), second.scratch("bare-venv")
        assert venv != other
        assert Path(venv).is_relative_to(first.root)
        assert Path(other).is_relative_to(second.root)

        detail = quickstart.python_trap(first, README)
        assert detail == f"{NOTE}bare uv venv used Python 3.9.6 and failed, as the README warns"
        managed = (Path(venv) / "managed-python-dir").read_text().strip()
        assert Path(managed).is_relative_to(first.root)
        assert not Path(other).exists()
    finally:
        first.close()
        second.close()

    assert not first.root.exists() and not second.root.exists()
    assert (outside / "keep").read_text() == "mine"


def test_bare_venv_commands_name_no_shared_paths(tmp_path: Path) -> None:
    env = Scripted(tmp_path, (1, "", "requires Python>=3.11"), (0, "Python 3.9.6\n", ""))
    quickstart.python_trap(env, README)
    commands = " ".join(env.commands)
    # Every path the commands touch is in the environment's scratch area (tmp_path
    # itself may live under /tmp, so compare prefixes rather than looking for "/tmp/").
    paths = set(re.findall(r"/[^\s'\";&|]+", commands)) - {"/dev/null"}
    assert paths and all(path.startswith(str(tmp_path)) for path in paths), paths
    assert "mktemp" not in commands and "rm -rf" not in commands
    assert env.scratch("bare-venv") in commands
    assert env.scratch("no-managed-python") in commands


class Server(quickstart.Environment):
    """Runs a Python snippet as the MCP server."""

    name = "server"

    def __init__(self, tmp_path: Path, code: str) -> None:
        self.tmp_path = tmp_path
        self.code = code
        self.argv: list[str] = []
        self.process: subprocess.Popen[str] | None = None

    def popen(self, argv: list[str]) -> subprocess.Popen[str]:
        self.argv = argv
        self.process = subprocess.Popen(
            [sys.executable, "-c", self.code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return self.process

    def workdir(self) -> str:
        return str(self.tmp_path)


SILENT = "import time\ntime.sleep(60)"
GONE = "pass"
WORKING = textwrap.dedent(
    """\
    import json, sys
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        if message["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}}
        else:
            names = ["discover", "validate", "compile", "execute"]
            result = {"tools": [{"name": name} for name in names]}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    """
)


def test_mcp_handshake_gives_up_on_a_server_that_never_answers(tmp_path: Path) -> None:
    env = Server(tmp_path, SILENT)
    started = time.monotonic()
    detail = quickstart.mcp_handshake(env, timeout=0.5)
    assert time.monotonic() - started < 10
    assert detail == "stdio handshake failed: no reply to initialize within 0.5s"
    assert env.process is not None and env.process.poll() is not None


def test_mcp_handshake_reports_a_server_that_exits(tmp_path: Path) -> None:
    detail = quickstart.mcp_handshake(Server(tmp_path, GONE), timeout=10)
    assert is_failure(detail) and detail.startswith("stdio handshake failed:")


def test_mcp_handshake_passes_a_server_that_lists_the_query_tools(tmp_path: Path) -> None:
    env = Server(tmp_path, WORKING)
    assert quickstart.mcp_handshake(env, timeout=10) == ""
    assert env.process is not None and env.process.poll() is not None
    # It starts the server the README registers, from the environment's workdir.
    package = f"{tmp_path}/my_package"
    assert env.argv == ["uvx", "semantic-rails", "mcp", "stdio", "--path", package]


def test_every_checked_command_is_in_the_readme() -> None:
    markdown = (quickstart.REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert quickstart.undocumented(markdown) == []


def test_commands_must_be_whole_lines_of_a_code_block() -> None:
    fenced = "```bash\n" + "\n".join(quickstart.DOCUMENTED) + "\n```\n"
    assert quickstart.undocumented(fenced) == []
    # A flag appended to a checked command, or a registration line without its `--`.
    extended = fenced.replace(quickstart.TRY, f"{quickstart.TRY} --limit 0")
    assert quickstart.undocumented(extended) == [quickstart.TRY]
    unseparated = fenced.replace("semantic-rails -- uvx", "semantic-rails uvx")
    assert quickstart.undocumented(unseparated) == [quickstart.CLAUDE_ADD, quickstart.CODEX_ADD]
    # A command quoted in prose doesn't count.
    assert quickstart.TRY in quickstart.undocumented(f"Run `{quickstart.TRY}`.")
    # The venv lines must stay together, in order.
    split = fenced.replace("source .venv/bin/activate\n", "")
    assert quickstart.undocumented(split) == [quickstart.VENV_BLOCK]


def test_a_bootstrap_timeout_removes_the_container(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:2] == ["docker", "exec"]:
            raise subprocess.TimeoutExpired(argv, 600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(quickstart.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        quickstart.Container("ubuntu:24.04")
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[-1] == ["docker", "rm", "-f", name]
