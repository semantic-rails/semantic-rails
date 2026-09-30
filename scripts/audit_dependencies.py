"""Audit locked core/extra installs with bounded, self-invalidating exceptions."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

CONNECTORS = ("snowflake", "postgres", "bigquery", "databricks", "athena", "clickhouse")
ROOT = Path(__file__).resolve().parents[1]


def audit_surfaces(root: Path) -> tuple[str, ...]:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    return ("core", *(extra for extra in project["optional-dependencies"] if extra != "all"))


def normalize(package: str) -> str:
    return re.sub(r"[-_.]+", "-", package).lower()


@dataclass(frozen=True)
class ExceptionEntry:
    id: str
    package: str
    extras: list[str]
    blocked_by: str
    fixed_in: str
    issue: str
    review_by: date
    reason: str


def load_exceptions(path: Path, today: date) -> list[ExceptionEntry]:
    entries = []
    seen = set()
    document = tomllib.loads(path.read_text())
    if set(document) != {"exceptions"} or not isinstance(document["exceptions"], list):
        raise ValueError("exceptions file must contain an exceptions array")
    for row in document["exceptions"]:
        entry = ExceptionEntry(**row)
        for field in ("id", "package", "blocked_by", "fixed_in", "issue", "reason"):
            value = getattr(entry, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"exception {field} must be a nonempty string")
        if entry.id in seen:
            raise ValueError(f"duplicate exception: {entry.id}")
        seen.add(entry.id)
        if (
            not isinstance(entry.extras, list)
            or not entry.extras
            or any(extra not in CONNECTORS for extra in entry.extras)
            or len(set(entry.extras)) != len(entry.extras)
        ):
            raise ValueError(f"{entry.id}: extras must be distinct connector names")
        if type(entry.review_by) is not date or entry.review_by > today + timedelta(days=30):
            raise ValueError(f"{entry.id}: review_by must be a date at most 30 days out")
        if not entry.issue.startswith("https://"):
            raise ValueError(f"{entry.id}: issue must be an HTTPS link")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", entry.package):
            raise ValueError(f"{entry.id}: invalid package name")
        blocker, separator, constraint = entry.blocked_by.partition(":")
        if (
            not separator
            or not constraint.strip()
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", blocker.strip())
        ):
            raise ValueError(f"{entry.id}: blocked_by must name a package and its cap")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*(?:[a-zA-Z]+[0-9]*)?", entry.fixed_in):
            raise ValueError(f"{entry.id}: invalid fixed_in version")
        entries.append(entry)
    return entries


def run(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=120)


def audit_surface(root: Path, surface: str, scratch: Path) -> list[dict]:
    requirements = scratch / f"{surface}.txt"
    command = [
        "uv",
        "export",
        "--locked",
        "--quiet",
        "--format",
        "requirements-txt",
        "--no-emit-project",
        "--no-dev",
    ]
    if surface != "core":
        command.extend(["--extra", surface])
    exported = run(command, root)
    if exported.returncode:
        raise ValueError(f"{surface}: export failed: {exported.stderr.strip()}")
    requirements.write_text(exported.stdout)
    audited = run(
        [
            sys.executable,
            "-m",
            "pip_audit",
            "-r",
            str(requirements),
            "--strict",
            "--disable-pip",
            "--no-deps",
            "--format",
            "json",
            "--progress-spinner",
            "off",
            "--cache-dir",
            str(scratch / "audit-cache"),
        ],
        root,
    )
    if audited.returncode not in (0, 1):
        raise ValueError(f"{surface}: audit failed: {audited.stderr.strip()}")
    dependencies = json.loads(audited.stdout)["dependencies"]
    if not isinstance(dependencies, list) or not dependencies:
        raise ValueError(f"{surface}: empty or invalid audit report")
    for dependency in dependencies:
        # pip-audit can emit a skipped package in JSON; never accept partial coverage.
        if "skip_reason" in dependency or not isinstance(dependency["vulns"], list):
            raise ValueError(f"{surface}: incomplete audit for {dependency['name']}")
    if audited.returncode == 1 and not any(dep["vulns"] for dep in dependencies):
        raise ValueError(f"{surface}: audit failed without findings: {audited.stderr.strip()}")
    return dependencies


def fix_resolves(
    root: Path, extra: str, entry: ExceptionEntry, patched_version: str, scratch: Path
) -> bool:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    requirements = scratch / "fixed.in"
    # Resolve current published ranges, never locked versions or overrides: a new
    # connector release lifting the cap must invalidate the old exception.
    requirements.write_text(
        "\n".join(
            [
                *project["dependencies"],
                *project["optional-dependencies"][extra],
                f"{entry.package}>={patched_version}",
            ]
        )
        + "\n"
    )
    try:
        resolved = run(
            [
                "uv",
                "pip",
                "compile",
                str(requirements),
                "--no-config",
                "--refresh",
                "--no-build",
                "--default-index",
                "https://pypi.org/simple",
                "--color",
                "never",
                "--python-version",
                f"{sys.version_info.major}.{sys.version_info.minor}",
            ],
            root,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"{extra}: cannot verify the cap: {error}") from error
    if resolved.returncode == 0:
        return True
    conflict = resolved.stderr.partition("No solution found")[2]
    blocker = normalize(entry.blocked_by.partition(":")[0].strip())
    named_packages = {normalize(token) for token in re.findall(r"[A-Za-z0-9_.-]+", conflict)}
    if resolved.returncode == 1 and conflict and blocker in named_packages:
        return False
    raise ValueError(f"{extra}: cannot verify the cap: {resolved.stderr.strip()}")


def check_policy(
    reports: dict[str, list[dict]],
    entries: list[ExceptionEntry],
    today: date,
    resolves: Callable[[str, ExceptionEntry, str], bool],
) -> tuple[list[str], list[str]]:
    """Only reported connector advisories with a still-blocked fix can be excepted."""
    errors = []
    lines = []
    used: set[tuple[str, str]] = set()
    core_packages = {normalize(dep["name"]) for dep in reports["core"]}
    for entry in entries:
        if normalize(entry.package) in core_packages:
            errors.append(f"{entry.id}: package reachable from core; exceptions forbidden")
        if entry.review_by < today:
            errors.append(f"{entry.id}: expired review_by {entry.review_by}")
    for surface in reports:
        findings = 0
        for dependency in reports[surface]:
            for advisory in dependency["vulns"]:
                findings += 1
                ids = {advisory["id"], *advisory.get("aliases", [])}
                matches = [
                    entry
                    for entry in entries
                    if entry.id in ids
                    and normalize(entry.package) == normalize(dependency["name"])
                    and surface in entry.extras
                ]
                prefix = f"[{surface}] {dependency['name']} {advisory['id']}"
                if surface not in CONNECTORS or len(matches) != 1:
                    errors.append(f"{prefix}: unexcepted advisory")
                    lines.append(f"{prefix}: FAIL")
                else:
                    entry = matches[0]
                    used.add((entry.id, surface))
                    fixes = advisory.get("fix_versions")
                    if (
                        not isinstance(fixes, list)
                        or not all(isinstance(version, str) for version in fixes)
                        or entry.fixed_in not in fixes
                    ):
                        errors.append(f"{prefix}: fixed_in missing from advisory patched versions")
                        continue
                    if any(resolves(surface, entry, version) for version in fixes):
                        errors.append(f"{prefix}: fix is resolvable; upgrade now")
                    lines.append(
                        f"{prefix}: listed exception until {entry.review_by} ({entry.issue})"
                    )
        if not findings:
            lines.append(f"[{surface}] clean")
    for entry in entries:
        for extra in entry.extras:
            if (entry.id, extra) not in used:
                errors.append(f"{entry.id}: unused exception for {extra}; remove it")
    return lines, errors


def main() -> int:
    try:
        today = date.today()
        entries = load_exceptions(ROOT / "security/audit-exceptions.toml", today)
        with tempfile.TemporaryDirectory(prefix="sr-dependency-audit-") as directory:
            scratch = Path(directory)
            reports = {
                surface: audit_surface(ROOT, surface, scratch) for surface in audit_surfaces(ROOT)
            }
            lines, errors = check_policy(
                reports,
                entries,
                today,
                lambda extra, entry, version: fix_resolves(ROOT, extra, entry, version, scratch),
            )
        for line in lines:
            print(line)
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return int(bool(errors))
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: dependency audit incomplete: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
