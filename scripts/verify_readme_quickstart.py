#!/usr/bin/env python3
"""Run the README's published-package quickstart in clean environments.

The commands below install the latest release from PyPI, not this checkout, so the
check shows whether the README works for a new user today. Each command must appear
verbatim in README.md: editing one without the other fails the check.

Environments:
  --image IMAGE  a fresh container (`docker run`), removed afterwards; repeatable.
                 CI uses ubuntu:22.04, ubuntu:24.04 and python:3.12-slim.
  --local        this machine, with a scratch HOME, an empty uv cache and PATH limited
                 to /usr/bin:/bin plus uv. On macOS this is the stock-macOS check: the
                 only Python on PATH is Apple's /usr/bin/python3.

The README warns about two traps. While a trap still reproduces, its warning must stay
in the README; when it stops reproducing, the check says so without failing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from functools import partial
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UV_VERSION = "0.12.17"
STEP_TIMEOUT_SECONDS = 600

TRY = 'uvx semantic-rails ask --package jaffle_shop "revenue by store" --run'
INIT = "uvx semantic-rails init my_package --yes"
VALIDATE = "uvx semantic-rails project validate --path ./my_package"
ASK = 'uvx semantic-rails ask --path ./my_package "total amount by event type" --run'
TOOL_INSTALL = "uv tool install semantic-rails"
VENV_BLOCK = "uv venv --python 3.12\nsource .venv/bin/activate\nuv pip install semantic-rails"
MCP_STDIO = 'uvx semantic-rails mcp stdio --path "$PWD/my_package"'
DOCUMENTED = (TRY, INIT, VALIDATE, ASK, TOOL_INSTALL, VENV_BLOCK, MCP_STDIO)

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
        bootstrap = self.run(BOOTSTRAP)
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

    def close(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.id], capture_output=True, check=False)


class Local(Environment):
    def __init__(self) -> None:
        uv, uvx = shutil.which("uv"), shutil.which("uvx")
        if not uv or not uvx:
            raise RuntimeError("--local needs uv and uvx on PATH")
        self.name = f"local ({sys.platform})"
        self.root = Path(tempfile.mkdtemp(prefix="sr-quickstart-"))
        for directory in ("home", "bin", "work", "cache"):
            (self.root / directory).mkdir()
        (self.root / "bin" / "uv").symlink_to(uv)
        (self.root / "bin" / "uvx").symlink_to(uvx)
        self.env = {
            "HOME": str(self.root / "home"),
            "PATH": f"{self.root / 'bin'}:/usr/bin:/bin",
            "UV_CACHE_DIR": str(self.root / "cache"),
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

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def report(env: Environment, command: str) -> dict[str, object]:
    """Run a documented `ask` command with --json and return its report ({} if unusable)."""
    result = env.run(f"{command} --json")
    try:
        parsed = json.loads(result.stdout)
    except ValueError:
        return {}
    return parsed if result.returncode == 0 and isinstance(parsed, dict) else {}


def answered_from(parsed: dict[str, object]) -> tuple[str, int]:
    """The package an `ask --json` report answered from, and how many rows it returned."""
    package = parsed.get("package")
    result = parsed.get("result")
    package_id = str(package.get("id", "")) if isinstance(package, dict) else ""
    rows = result.get("rows") if isinstance(result, dict) else None
    return package_id, len(rows) if isinstance(rows, list) else 0


def expect(result: subprocess.CompletedProcess[str], *needles: str) -> str:
    output = result.stdout + result.stderr
    if result.returncode:
        return f"exit {result.returncode}: {output.strip()[-400:]}"
    missing = [needle for needle in needles if needle not in output]
    return f"output lacks {missing}" if missing else ""


def mcp_handshake(env: Environment) -> str:
    """Initialize the stdio server the README registers with agents and list its tools."""
    package = f"{env.workdir()}/my_package"
    argv = ["uvx", "semantic-rails", "mcp", "stdio", "--path", package]
    if isinstance(env, Local):
        argv[0] = str(env.root / "bin" / "uvx")
    process = env.popen(argv)
    assert process.stdin is not None and process.stdout is not None

    def call(message: dict[str, object]) -> dict[str, object]:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        while True:
            line = process.stdout.readline()
            if not line:
                raise RuntimeError("server closed stdout")
            reply: dict[str, object] = json.loads(line)
            if reply.get("id") == message["id"]:
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
    except (RuntimeError, ValueError, KeyError, TypeError) as error:
        return f"stdio handshake failed: {error}"
    finally:
        process.kill()
        process.wait(timeout=30)


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
        package, count = answered_from(report(env, TRY))
        if problem or package != "jaffle_shop" or not count:
            return problem or f"answered from {package or 'nothing'} with {count} rows"
        return ""

    def own_package() -> str:
        for command, needles in ((INIT, ()), (VALIDATE, ("my_package",)), (ASK, ())):
            result = env.run(command)
            problem = expect(result, *needles)
            if problem:
                return f"{command}: {problem}"
            if "[fail]" in result.stdout:
                return f"{command}: reported a failed check"
        package, count = answered_from(report(env, ASK))
        if package != "my_package" or not count:
            return f"ask --path answered from {package or 'nothing'} with {count} rows"
        return ""

    def fallback_trap() -> str:
        package, _ = answered_from(report(env, ASK_WITHOUT_PATH))
        if package != "jaffle_shop":
            return f"{NOTE}ask without --path no longer falls back: drop the README warning"
        return "" if FALLBACK_WARNING in readme else "fallback reproduces; README lacks its warning"

    def python_trap() -> str:
        # Minimal images ship no Python; a typical host has the distro's python3. An empty
        # managed-Python directory hides the interpreters earlier steps downloaded, as on a
        # machine that has never run uv.
        result = env.run(
            "if ! command -v python3 >/dev/null && command -v apt-get >/dev/null; then "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 >/dev/null; fi; "
            'export UV_PYTHON_INSTALL_DIR="$(mktemp -d)"; '
            "rm -rf /tmp/bare-venv && uv venv /tmp/bare-venv >/dev/null 2>&1 && "
            "uv pip install -q --python /tmp/bare-venv/bin/python semantic-rails"
        )
        found = env.run("/tmp/bare-venv/bin/python --version").stdout.strip()
        if result.returncode == 0:
            return f"{NOTE}bare uv venv used {found}; the install worked"
        if "Python>=3.11" not in result.stdout + result.stderr:
            return f"bare uv venv failed unexpectedly: {(result.stdout + result.stderr)[-300:]}"
        if PYTHON_WARNING not in readme:
            return f"bare uv venv used {found} and failed; README lacks its warning"
        return f"{NOTE}bare uv venv used {found} and failed, as the README warns"

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
    step("trap: ask without --path", fallback_trap)
    step("trap: bare uv venv", python_trap)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", action="append", default=[], help="container image to test")
    parser.add_argument("--local", action="store_true", help="also test this machine")
    parser.add_argument("--json", type=Path, help="write the results as JSON")
    args = parser.parse_args(argv)
    if not args.image and not args.local:
        parser.error("choose at least one --image or --local")

    # Compare with whitespace collapsed: README prose wraps mid-sentence.
    readme = " ".join((REPO_ROOT / "README.md").read_text(encoding="utf-8").split())
    missing = [command for command in DOCUMENTED if " ".join(command.split()) not in readme]
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
