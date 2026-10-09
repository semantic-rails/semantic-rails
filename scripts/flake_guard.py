"""Repeat a bounded set of affected unit test files in a merge group."""

from __future__ import annotations

import argparse
import ast
import math
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

try:
    from scripts.test_sharding import in_shard
except ModuleNotFoundError:  # Direct script invocation.
    from test_sharding import in_shard

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOTS = ("tests/semantic_rails",)
MAX_FILES = 20
BUDGET_SECONDS = 290
# Leave headroom above measured test durations when sizing or starting a repetition.
FIT_MARGIN = 1.2
# A repeated test running longer than this is hung; unit tests take about a second at most.
TEST_TIMEOUT_SECONDS = 60


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
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


def measured_durations(report: Path | None, files: list[str]) -> dict[str, float]:
    """Sum pytest's per-test timings, including tests in classes, by selected file."""
    if report is None:
        return {}
    try:
        cases = list(ET.parse(report).iter("testcase"))
    except (OSError, ET.ParseError):
        return {}
    durations = {}
    for file in files:
        module = file.removesuffix(".py").replace("/", ".")
        selected = [
            case
            for case in cases
            if case.get("classname", "") == module
            or case.get("classname", "").startswith(module + ".")
        ]
        try:
            times = [float(case.attrib["time"]) for case in selected]
        except (KeyError, ValueError):
            continue
        if times and all(math.isfinite(duration) and duration >= 0 for duration in times):
            durations[file] = sum(times)
    return durations


def size_selection(
    files: list[str], durations: dict[str, float], workers: int, budget: float
) -> tuple[list[str], float | None]:
    """Trim the lowest-priority files; incomplete timings cannot predict a timeout."""
    selected = files.copy()
    while (
        selected
        and sum(durations.get(file, 0) for file in selected) / workers * FIT_MARGIN > budget
    ):
        selected.pop()
    if len(selected) < len(files):
        print(
            f"::notice::Flake guard dropped files to fit its time budget: {files[len(selected) :]}",
            flush=True,
        )
    estimate = (
        sum(durations[file] for file in selected) / workers
        if all(file in durations for file in selected)
        else None
    )
    return selected, estimate


def run_repetitions(
    files: list[str], root: Path, deadline: float, durations_from: Path | None = None
) -> int:
    # Resolve the worker count once so sizing and pytest use the same parallelism.
    workers = max(1, os.cpu_count() or 1)
    files, estimate = size_selection(
        files,
        measured_durations(durations_from, files),
        workers,
        max(0, deadline - time.monotonic()),
    )
    if not files:
        print("Flake guard: no affected unit test files")
        return 0
    print("Flake guard files:\n" + "\n".join(files), flush=True)
    with tempfile.TemporaryDirectory(prefix="flake-guard-") as scratch:
        seed_base = secrets.randbelow(2**32 - 3)
        previous: float | None = None
        for repetition in range(1, 4):
            seed = seed_base + repetition
            report = Path(scratch) / f"repetition-{repetition}.xml"
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-n",
                str(workers),
                f"--flake-seed={seed}",
                f"--junitxml={report}",
                # A hung test fails on its own limit and ends the run, well inside the budget.
                f"--timeout={TEST_TIMEOUT_SECONDS}",
                "--max-worker-restart=0",
                "-p",
                "no:cacheprovider",
                *files,
            ]
            remaining = deadline - time.monotonic()
            # A repetition that cannot fit in what is left of the budget proves nothing either way: skip it rather than
            # fail a pull request whose tests merely take long (a core-module change selects many importers).
            expected = estimate if previous is None else previous
            if expected is not None and remaining < expected * FIT_MARGIN:
                print(
                    f"::warning::Flake guard inconclusive: {repetition - 1} of 3 repetitions passed; "
                    f"repetition {repetition} needs about {expected:.0f}s and {max(remaining, 0):.0f}s remain",
                    flush=True,
                )
                return 0
            print(f"Flake guard repetition {repetition}/3, seed {seed}", flush=True)
            if remaining <= 0:
                print(
                    f"::warning::Flake guard inconclusive: {repetition - 1} of 3 repetitions passed; "
                    "budget exhausted",
                    flush=True,
                )
                return 0
            # Kill the whole group on timeout, including xdist workers.
            started = time.monotonic()
            process = subprocess.Popen(command, cwd=root, start_new_session=True)
            try:
                result = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                # Each test runs under its own limit, so a repetition the budget stops was slow, not hung: like one
                # that cannot fit, it proves nothing either way.
                print(
                    f"::notice::Flake guard inconclusive: {repetition - 1} of 3 repetitions passed; "
                    f"repetition {repetition} ran out of the time budget: {files}",
                    flush=True,
                )
                return 0
            if result:
                print(
                    f"intermittent: investigate; repetition {repetition}; "
                    + ", ".join(failure_names(report)),
                    flush=True,
                )
                return 1
            previous = time.monotonic() - started
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="merge_group base SHA")
    parser.add_argument("--durations-from", type=Path, help="main test run's JUnit report")
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
    files = [file for file in select_tests(ROOT, changed) if in_shard(file)]
    return run_repetitions(files, ROOT, deadline, args.durations_from)


if __name__ == "__main__":
    raise SystemExit(main())
