#!/usr/bin/env python3
"""Run the README's published-package quickstart in clean environments.

The commands below install the latest release from PyPI, not this checkout, so the
check shows whether the README works for a new user today. Each command must appear
verbatim, as whole lines of a README code block: editing one without the other fails
the check. The Claude Code and Codex registration lines are held to the same rule, and
the check starts the stdio server they register. Claude Desktop's `mcp setup`, the
Cursor config and the MetricFlow import are not run here.

Environments:
  --image IMAGE  a fresh container (`docker run`), removed afterwards; repeatable.
                 CI uses ubuntu:22.04, ubuntu:24.04 and python:3.12-slim.
  --local        this machine, with a scratch HOME, an empty uv cache and PATH limited
                 to /usr/bin:/bin plus uv. On macOS this is the stock-macOS check: the
                 only Python on PATH is Apple's /usr/bin/python3.

The README warns about two traps. While a trap still reproduces, its warning must stay
in the README; when it stops reproducing, the check says so without failing. Any other
outcome (a crash, a report that isn't JSON, an unexpected package) fails, so a broken
run is never read as a fixed trap. Everything the check creates stays inside the
environment it runs in and is removed with it.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UV_VERSION = "0.12.17"
STEP_TIMEOUT_SECONDS = 600
MCP_TIMEOUT_SECONDS = 120

TRY = 'uvx semantic-rails ask --package jaffle_shop "revenue by store" --run'
INIT = "uvx semantic-rails init my_package --yes"
VALIDATE = "uvx semantic-rails project validate --path ./my_package"
ASK = 'uvx semantic-rails ask --path ./my_package "total amount by event type" --run'
TOOL_INSTALL = "uv tool install semantic-rails"
VENV_BLOCK = "uv venv --python 3.12\nsource .venv/bin/activate\nuv pip install semantic-rails"
MCP_STDIO = 'uvx semantic-rails mcp stdio --path "$PWD/my_package"'
# The README registers MCP_STDIO with these clients; the check runs the server they start.
CLAUDE_ADD = f"claude mcp add semantic-rails -- {MCP_STDIO}"
CODEX_ADD = f"codex mcp add semantic-rails -- {MCP_STDIO}"
DOCUMENTED = (TRY, INIT, VALIDATE, ASK, TOOL_INSTALL, VENV_BLOCK, CLAUDE_ADD, CODEX_ADD)

ASK_WITHOUT_PATH = 'uvx semantic-rails ask "total amount by event type" --run'
FALLBACK_WARNING = "commands fall back to the bundled `jaffle_shop` package"
PYTHON_WARNING = "a bare `uv venv` can pick up the system Python"
NOTE = "note: "

BOOTSTRAP = (
    "set -e; "
    "if command -v apt-get >/dev/null; then "
    "apt-get update -qq && DEBIAN_FRONTEND=noninteractive "
    "apt-get install -y -qq curl ca-certificates >/dev/null; fi; "
    f"curl -LsSf https://astral.sh/uv/{UV_VERSION}/install.sh "
    "| env UV_UNMANAGED_INSTALL=/usr/local/bin sh >/dev/null"
)


class Environment:
    """Run shell commands in one clean environment, from a scratch working directory."""

    name: str

    def run(self, command: str) -> subprocess.CompletedProcess[str]:
        raise NotImplementedError

    def popen(self, argv: list[str]) -> subprocess.Popen[str]:
        raise NotImplementedError

    def workdir(self) -> str:
        raise NotImplementedError

    def scratch(self, name: str) -> str:
        """A path for the check's own files, which close() removes with the environment."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class Container(Environment):
    def __init__(self, image: str) -> None:
        self.name = image
        self.id = f"sr-quickstart-{uuid.uuid4().hex[:10]}"
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                self.id,
                "-w",
                "/work",
                image,
                "sleep",
                "infinity",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            bootstrap = self.run(BOOTSTRAP)
        except BaseException:
            # A timeout here must not leave a `sleep infinity` container behind.
            self.close()
            raise
        if bootstrap.returncode:
            self.close()
            raise RuntimeError(
                f"{image}: bootstrap failed:\n{(bootstrap.stdout + bootstrap.stderr)[-2000:]}"
            )

    def run(self, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "exec", "-w", "/work", self.id, "bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=STEP_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )

    def popen(self, argv: list[str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["docker", "exec", "-i", "-w", "/work", self.id, *argv],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def workdir(self) -> str:
        return "/work"

    def scratch(self, name: str) -> str:
        # Inside the container, which close() removes.
        return f"/scratch/{name}"

    def close(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.id], capture_output=True, check=False)


class Local(Environment):
    def __init__(self) -> None:
        uv, uvx = shutil.which("uv"), shutil.which("uvx")
        if not uv or not uvx:
            raise RuntimeError("--local needs uv and uvx on PATH")
        self.name = f"local ({sys.platform})"
        self.root = Path(tempfile.mkdtemp(prefix="sr-quickstart-"))
        for directory in ("home", "bin", "work", "cache", "tmp"):
            (self.root / directory).mkdir()
        (self.root / "bin" / "uv").symlink_to(uv)
        (self.root / "bin" / "uvx").symlink_to(uvx)
        self.env = {
            "HOME": str(self.root / "home"),
            "PATH": f"{self.root / 'bin'}:/usr/bin:/bin",
            "UV_CACHE_DIR": str(self.root / "cache"),
            "TMPDIR": str(self.root / "tmp"),
            "TERM": "dumb",
        }

    def run(self, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", "-c", command],
            cwd=self.root / "work",
            env=self.env,
            capture_output=True,
            text=True,
            timeout=STEP_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )

    def popen(self, argv: list[str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            argv,
            cwd=self.root / "work",
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def workdir(self) -> str:
        return str(self.root / "work")

    def scratch(self, name: str) -> str:
        # Under this run's own temporary root, which close() removes.
        return str(self.root / "scratch" / name)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


@dataclass
class AskReport:
    """What `ask ... --json` did: its exit status, its JSON object (if any) and its output."""

    returncode: int
    data: dict[str, object] | None
    output: str

    def problem(self) -> str:
        """Why this isn't a usable answer, or "" if it is one."""
        if self.returncode:
            return f"exit {self.returncode}: {self.output.strip()[-300:]}"
        if self.data is None:
            return f"printed no JSON report: {self.output.strip()[-300:]}"
        return ""


def report(env: Environment, command: str) -> AskReport:
    """Run a documented `ask` command with --json and keep everything needed to judge it."""
    result = env.run(f"{command} --json")
    try:
        parsed = json.loads(result.stdout)
    except ValueError:
        parsed = None
    data = parsed if isinstance(parsed, dict) else None
    return AskReport(result.returncode, data, result.stdout + result.stderr)


def answered_from(parsed: dict[str, object] | None) -> tuple[str, int]:
    """The package an `ask --json` report answered from, and how many rows it returned."""
    parsed = parsed or {}
    package = parsed.get("package")
    result = parsed.get("result")
    package_id = str(package.get("id", "")) if isinstance(package, dict) else ""
    rows = result.get("rows") if isinstance(result, dict) else None
    return package_id, len(rows) if isinstance(rows, list) else 0


def no_package_selected(ask: AskReport) -> bool:
    """True for the engine's documented refusal when no package was chosen.

    Releases that drop the silent fallback exit non-zero and print
    {"ok": false, "error": {"code": "INVALID_CONFIG", "details": {"reason": "no_package_selected"}}}.
    """
    error = (ask.data or {}).get("error")
    if ask.returncode == 0 or not isinstance(error, dict):
        return False
    details = error.get("details")
    reason = details.get("reason") if isinstance(details, dict) else None
    return error.get("code") == "INVALID_CONFIG" and reason == "no_package_selected"


def expect(result: subprocess.CompletedProcess[str], *needles: str) -> str:
    output = result.stdout + result.stderr
    if result.returncode:
        return f"exit {result.returncode}: {output.strip()[-400:]}"
    missing = [needle for needle in needles if needle not in output]
    return f"output lacks {missing}" if missing else ""


def mcp_handshake(env: Environment, timeout: float = MCP_TIMEOUT_SECONDS) -> str:
    """Initialize the stdio server the README registers with agents and list its tools.

    The whole exchange has one deadline: a server that stays alive without answering
    fails this step instead of hanging the run.
    """
    # The command the README registers with Claude Code and Codex, run from its workdir.
    argv = shlex.split(MCP_STDIO.replace("$PWD", env.workdir()))
    if isinstance(env, Local):
        argv[0] = str(env.root / "bin" / "uvx")
    process = env.popen(argv)
    assert process.stdin is not None and process.stdout is not None
    deadline = time.monotonic() + timeout
    lines: queue.Queue[str] = queue.Queue()

    def pump(stdout: object) -> None:
        for line in stdout:  # type: ignore[attr-defined]
            lines.put(line)
        lines.put("")  # end of output

    threading.Thread(target=pump, args=(process.stdout,), daemon=True).start()

    def call(message: dict[str, object]) -> dict[str, object]:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        while True:
            try:
                line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError(f"no reply to {message['method']} within {timeout:g}s") from None
            if not line:
                raise RuntimeError("server closed stdout")
            reply = json.loads(line)
            if isinstance(reply, dict) and reply.get("id") == message["id"]:
                return reply

    try:
        call(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "readme-quickstart", "version": "1"},
                },
            }
        )
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        listed = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        result = listed.get("result")
        tools = [tool["name"] for tool in result["tools"]] if isinstance(result, dict) else []
        missing = {"discover", "validate", "compile", "execute"} - set(tools)
        return f"tools/list lacks {sorted(missing)}" if missing else ""
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        # OSError covers TimeoutError and a broken pipe to a server that exited.
        return f"stdio handshake failed: {error}"
    finally:
        process.kill()
        process.wait(timeout=30)


def fallback_trap(env: Environment, readme: str) -> str:
    """0.2.x answers `ask` without --path from the bundled sample package, exiting 0.

    Only two outcomes are understood: that fallback, or the engine's documented
    no-package refusal. Anything else (a crash, a non-JSON report, another package)
    fails, so a broken run is never mistaken for a fixed trap.
    """
    ask = report(env, ASK_WITHOUT_PATH)
    if not ask.problem():
        package, _ = answered_from(ask.data)
        if package == "jaffle_shop":
            if FALLBACK_WARNING in readme:
                return ""
            return "ask without --path falls back to jaffle_shop; README lacks its warning"
        return f"ask without --path answered from {package or 'no package'}, exit 0"
    if no_package_selected(ask):
        fixed = "ask without --path now refuses with no_package_selected"
        return f"{NOTE}{fixed}: drop the README warning" if FALLBACK_WARNING in readme else ""
    return f"ask without --path failed unexpectedly: {ask.problem()}"


def python_trap(env: Environment, readme: str) -> str:
    """A bare `uv venv` may pick an old system Python, and then the install fails."""
    # Minimal images ship no Python; a typical host has the distro's python3. An empty
    # managed-Python directory hides the interpreters earlier steps downloaded, as on a
    # machine that has never run uv. Both directories live in the environment's scratch
    # area, so nothing outside this run is touched.
    venv = shlex.quote(env.scratch("bare-venv"))
    pythons = shlex.quote(env.scratch("no-managed-python"))
    result = env.run(
        "if ! command -v python3 >/dev/null && command -v apt-get >/dev/null; then "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 >/dev/null; fi; "
        f"mkdir -p {pythons} && export UV_PYTHON_INSTALL_DIR={pythons} && "
        f"uv venv {venv} >/dev/null 2>&1 && "
        f"uv pip install -q --python {venv}/bin/python semantic-rails"
    )
    found = env.run(f"{venv}/bin/python --version").stdout.strip() or "no interpreter"
    if result.returncode == 0:
        return f"{NOTE}bare uv venv used {found}; the install worked"
    if "Python>=3.11" not in result.stdout + result.stderr:
        return f"bare uv venv failed unexpectedly: {(result.stdout + result.stderr)[-300:]}"
    if PYTHON_WARNING not in readme:
        return f"bare uv venv used {found} and failed; README lacks its warning"
    return f"{NOTE}bare uv venv used {found} and failed, as the README warns"


def code_blocks(markdown: str) -> list[list[str]]:
    """The lines of each fenced code block, with runs of whitespace collapsed."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
        elif current is not None:
            current.append(" ".join(line.split()))
    return blocks


def undocumented(markdown: str) -> list[str]:
    """Checked commands that aren't whole, consecutive lines of one README code block."""
    blocks = code_blocks(markdown)
    missing = []
    for command in DOCUMENTED:
        lines = [" ".join(line.split()) for line in command.splitlines()]
        if not any(
            block[start : start + len(lines)] == lines
            for block in blocks
            for start in range(len(block))
        ):
            missing.append(command)
    return missing


def run_checks(env: Environment, readme: str) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []

    def step(name: str, check: Callable[[], str]) -> None:
        started = time.monotonic()
        try:
            detail = check()
        except subprocess.TimeoutExpired:
            detail = f"timed out after {STEP_TIMEOUT_SECONDS}s"
        status = "note" if detail.startswith(NOTE) else ("fail" if detail else "pass")
        detail = detail.removeprefix(NOTE)
        results.append(
            {
                "environment": env.name,
                "step": name,
                "status": status,
                "seconds": round(time.monotonic() - started, 1),
                "detail": detail,
            }
        )

    def try_bundled() -> str:
        problem = expect(env.run(TRY))
        if problem:
            return problem
        ask = report(env, TRY)
        package, count = answered_from(ask.data)
        if ask.problem() or package != "jaffle_shop" or not count:
            return ask.problem() or f"answered from {package or 'nothing'} with {count} rows"
        return ""

    def own_package() -> str:
        for command, needles in ((INIT, ()), (VALIDATE, ("my_package",)), (ASK, ())):
            result = env.run(command)
            problem = expect(result, *needles)
            if problem:
                return f"{command}: {problem}"
            if "[fail]" in result.stdout:
                return f"{command}: reported a failed check"
        ask = report(env, ASK)
        package, count = answered_from(ask.data)
        if ask.problem() or package != "my_package" or not count:
            reason = ask.problem() or f"answered from {package or 'nothing'} with {count} rows"
            return f"ask --path --json: {reason}"
        return ""

    def pinned_venv() -> str:
        block = " && ".join(VENV_BLOCK.splitlines())
        result = env.run(f"rm -rf .venv && {block} && semantic-rails --version")
        return expect(result, "semantic-rails ")

    def tool_install() -> str:
        return expect(
            env.run(f"{TOOL_INSTALL} && ~/.local/bin/semantic-rails --version"), "semantic-rails "
        )

    step("try: bundled package", try_bundled)
    step("own package: init, validate, ask --path", own_package)
    step("mcp stdio: initialize, tools/list", lambda: mcp_handshake(env))
    step("install: uv venv --python 3.12", pinned_venv)
    step("install: uv tool install", tool_install)
    step("trap: ask without --path", partial(fallback_trap, env, readme))
    step("trap: bare uv venv", partial(python_trap, env, readme))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", action="append", default=[], help="container image to test")
    parser.add_argument("--local", action="store_true", help="also test this machine")
    parser.add_argument("--json", type=Path, help="write the results as JSON")
    args = parser.parse_args(argv)
    if not args.image and not args.local:
        parser.error("choose at least one --image or --local")

    markdown = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    # The warnings are prose, which wraps mid-sentence: compare with whitespace collapsed.
    readme = " ".join(markdown.split())
    missing = undocumented(markdown)
    if missing:
        for command in missing:
            print(f"README.md no longer contains: {command!r}", file=sys.stderr)
        return 1

    results: list[dict[str, object]] = []
    targets: list[Callable[[], Environment]] = [partial(Container, image) for image in args.image]
    if args.local:
        targets.append(Local)
    for make in targets:
        env = make()
        try:
            results.extend(run_checks(env, readme))
        finally:
            env.close()

    for row in results:
        print(
            f"{row['status']:4}  {row['environment']:18}  {row['step']:40}  "
            f"{row['seconds']:>6}s  {row['detail']}"
        )
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    failures = [row for row in results if row["status"] == "fail"]
    print(f"{len(results) - len(failures)} passed or noted, {len(failures)} failed")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for row in results:
            if row["status"] == "note":
                print(f"::notice title={row['step']}::{row['detail']}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
