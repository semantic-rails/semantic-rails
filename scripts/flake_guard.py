"""Repeat a bounded set of affected unit test files in a merge group."""

from __future__ import annotations

import argparse
import ast
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOTS = ("tests/semantic_rails", "tests/mf2sr")
MAX_FILES = 20
BUDGET_SECONDS = 290


def imported_modules(path: Path) -> set[str]:
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.update(
                node.module if alias.name == "*" else f"{node.module}.{alias.name}"
                for alias in node.names
            )
    return modules


def select_tests(root: Path, changed: list[str], cap: int = MAX_FILES) -> list[str]:
    """Prioritize changed files, then direct importers of changed runtime modules."""
    available = {
        path.relative_to(root).as_posix(): path
        for directory in TEST_ROOTS
        for path in (root / directory).rglob("test_*.py")
    }
    direct = sorted(available.keys() & set(changed))
    modules = {
        path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        for path in changed
        if path.startswith("semantic_rails/") and path.endswith(".py")
    }
    importers = []
    if modules:
        for name, path in sorted(available.items()):
            if name in direct:
                continue
            if any(
                imported == module
                or imported.startswith(module + ".")
                or module.startswith(imported + ".")
                for imported in imported_modules(path)
                for module in modules
            ):
                importers.append(name)
    selected = (direct + importers)[:cap]
    print(f"Flake guard: {len(direct)} changed files, {len(importers)} importers; cap {cap}")
    if len(direct) + len(importers) > cap:
        print(f"Flake guard: capped out {len(direct) + len(importers) - cap} files")
    return selected


def failure_names(report: Path) -> list[str]:
    try:
        cases = list(ET.parse(report).iter("testcase"))
    except (OSError, ET.ParseError):
        return ["pytest process (no complete report)"]
    return [
        f"{case.get('classname')}::{case.get('name')}"
        for case in cases
        if case.find("failure") is not None or case.find("error") is not None
    ] or ["pytest collection or worker process"]


def run_repetitions(files: list[str], root: Path, deadline: float) -> int:
    if not files:
        print("Flake guard: no affected unit test files")
        return 0
    print("Flake guard files:\n" + "\n".join(files), flush=True)
    with tempfile.TemporaryDirectory(prefix="flake-guard-") as scratch:
        seed_base = secrets.randbelow(2**32 - 3)
        for repetition in range(1, 4):
            seed = seed_base + repetition
            report = Path(scratch) / f"repetition-{repetition}.xml"
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-n",
                "auto",
                f"--flake-seed={seed}",
                f"--junitxml={report}",
                "-p",
                "no:cacheprovider",
                *files,
            ]
            print(f"Flake guard repetition {repetition}/3, seed {seed}", flush=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(
                    f"intermittent: investigate; repetition {repetition}; budget exhausted",
                    flush=True,
                )
                return 1
            # Kill the whole group on timeout, including xdist workers.
            process = subprocess.Popen(command, cwd=root, start_new_session=True)
            try:
                result = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                print(
                    f"intermittent: investigate; repetition {repetition}; timed out: {files}",
                    flush=True,
                )
                return 1
            if result:
                print(
                    f"intermittent: investigate; repetition {repetition}; "
                    + ", ".join(failure_names(report)),
                    flush=True,
                )
                return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="merge_group base SHA")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.base):
        parser.error("--base must be a full commit SHA")
    deadline = time.monotonic() + BUDGET_SECONDS
    changed = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "--diff-filter=ACDM",
            args.base,
            "HEAD",
            "--",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout.splitlines()
    return run_repetitions(select_tests(ROOT, changed), ROOT, deadline)


if __name__ == "__main__":
    raise SystemExit(main())
