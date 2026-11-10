"""Audit locked core/extra installs with bounded, self-invalidating exceptions.

With --base REV, fail only on findings absent at that revision (diff mode), so an advisory
published against an existing dependency fails main and the nightly audit, not every open
pull request. Findings the base already has are printed as PRE-EXISTING.
"""

from __future__ import annotations

import argparse
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
from urllib.request import urlopen

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

CONNECTORS = ("snowflake", "postgres", "bigquery", "databricks", "athena", "clickhouse")
ROOT = Path(__file__).resolve().parents[1]
EXCEPTIONS = "security/audit-exceptions.toml"
# The audit's inputs: a base revision with identical files has identical findings.
INPUTS = ("pyproject.toml", "uv.lock", EXCEPTIONS)
FIX_PROCESS = "CONTRIBUTING.md#dependency-advisories"
INCOMPLETE = (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired)


def audit_surfaces(root: Path) -> tuple[str, ...]:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    optional = project["optional-dependencies"]

    def requirements(values: list[str]) -> set[tuple]:
        return {
            (
                normalize(req.name),
                frozenset(normalize(extra) for extra in req.extras),
                req.specifier,
                req.url,
                str(req.marker) if req.marker else None,
            )
            for req in map(Requirement, values)
        }

    if "all" in optional and requirements(optional["all"]) != requirements(
        [value for extra, values in optional.items() if extra != "all" for value in values]
    ):
        raise ValueError("all must equal the union of the other extras")
    return ("core", *(extra for extra in optional if extra != "all"))


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


def caps_below(specifiers: SpecifierSet, patched: Version) -> bool:
    """Prove an upper bound below all patched releases; exclusions cannot prove a cap."""
    capped = False
    for specifier in specifiers:
        operator = specifier.operator
        version = Version(specifier.version.removesuffix(".*"))
        if operator == "~=" or (operator == "==" and specifier.version.endswith(".*")):
            prefix = version.release[:-1] if operator == "~=" else version.release
            upper = Version(
                f"{version.epoch}!" + ".".join(map(str, (*prefix[:-1], prefix[-1] + 1)))
            )
            capped |= upper <= patched
        elif operator == "<":
            capped |= version <= patched
        elif operator in ("<=", "==", "==="):
            capped |= version < patched
        elif operator not in (">=", ">", "!="):
            raise ValueError(f"unknown dependency operator: {operator}")
    return capped


def cap_excludes_fixes(
    root: Path, extra: str, entry: ExceptionEntry, patched_versions: list[str]
) -> bool:
    """Only an active dependency cap in the latest blocker release permits an exception."""
    blocker = normalize(entry.blocked_by.partition(":")[0].strip())
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        # Dependency markers use the blocker's requested extras, not our connector's name.
        selected_extras = set()
        for value in [*project["dependencies"], *project["optional-dependencies"][extra]]:
            requirement = Requirement(value)
            if normalize(requirement.name) == blocker and (
                requirement.marker is None or requirement.marker.evaluate({"extra": extra})
            ):
                selected_extras.add("")
                selected_extras.update(requirement.extras)
        if not selected_extras:
            raise ValueError(f"{blocker} is not a dependency of {extra}")
        with urlopen(f"https://pypi.org/pypi/{blocker}/json", timeout=30) as response:
            info = json.load(response)["info"]
        if normalize(info["name"]) != blocker:
            raise ValueError("metadata names a different package")
        Version(info["version"])
        values = info["requires_dist"]
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError("invalid requires_dist metadata")
        requirements = [Requirement(value) for value in values]
        patched = min(Version(version) for version in patched_versions)
        caps = [
            requirement
            for requirement in requirements
            if normalize(requirement.name) == normalize(entry.package)
            and (
                requirement.marker is None
                or any(requirement.marker.evaluate({"extra": name}) for name in selected_extras)
            )
        ]
        if not caps or any(requirement.url for requirement in caps):
            raise ValueError("missing verifiable dependency requirement")
        # Evaluate every active requirement so malformed evidence cannot be bypassed.
        capped = any([caps_below(requirement.specifier, patched) for requirement in caps])
        combined = SpecifierSet()
        for requirement in caps:
            combined &= requirement.specifier
        return capped and not any(
            combined.contains(version, prereleases=True) for version in patched_versions
        )
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError(f"{extra}: cannot verify the cap: {error}") from error


def check_policy(
    reports: dict[str, list[dict]],
    entries: list[ExceptionEntry],
    today: date,
    cap_excludes: Callable[[str, ExceptionEntry, list[str]], bool],
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
        packages = {normalize(dep["name"]) for dep in reports[surface]}
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
                    blocker = normalize(entry.blocked_by.partition(":")[0].strip())
                    if blocker not in packages:
                        errors.append(
                            f"{prefix}: cannot verify the cap: "
                            f"{blocker} is not a dependency of {surface}"
                        )
                        lines.append(f"{prefix}: FAIL")
                        continue
                    fixes = advisory.get("fix_versions")
                    if (
                        not isinstance(fixes, list)
                        or not all(isinstance(version, str) for version in fixes)
                        or entry.fixed_in not in fixes
                    ):
                        errors.append(f"{prefix}: fixed_in missing from advisory patched versions")
                        lines.append(f"{prefix}: FAIL")
                        continue
                    if not cap_excludes(surface, entry, fixes):
                        errors.append(f"{prefix}: cap lifted: upgrade now")
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


def python_audit(root: Path, today: date, scratch: Path) -> tuple[list[str], list[str]]:
    """Return the report lines and the policy errors for the audit inputs under root."""
    entries = load_exceptions(root / EXCEPTIONS, today)
    reports = {surface: audit_surface(root, surface, scratch) for surface in audit_surfaces(root)}
    return check_policy(
        reports,
        entries,
        today,
        lambda extra, entry, versions: cap_excludes_fixes(root, extra, entry, versions),
    )


def base_inputs(root: Path, base: str, scratch: Path) -> Path | None:
    """Write the base revision's audit inputs under scratch; None when they equal the head's."""
    tree = scratch / "base"
    same = True
    for path in INPUTS:
        shown = run(["git", "show", f"{base}:{path}"], root)
        if shown.returncode:
            raise ValueError(f"cannot read {path} at {base}: {shown.stderr.strip()}")
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        (tree / path).write_text(shown.stdout)
        same = same and (root / path).read_text() == shown.stdout
    return None if same else tree


def main(base: str | None = None) -> int:
    """Full mode fails on every finding; diff mode only on findings the base lacks."""
    today = date.today()
    existing: set[str] = set()
    try:
        with tempfile.TemporaryDirectory(prefix="sr-dependency-audit-") as directory:
            scratch = Path(directory)
            lines, errors = python_audit(ROOT, today, scratch)
            if base and errors:
                try:
                    tree = base_inputs(ROOT, base, scratch)
                    existing = set(
                        errors if tree is None else python_audit(tree, today, scratch)[1]
                    )
                except INCOMPLETE as error:
                    print(
                        f"Base {base} audit unavailable ({error}); every finding counts as new.",
                        file=sys.stderr,
                    )
    except INCOMPLETE as error:
        print(f"FAIL: dependency audit incomplete: {error}", file=sys.stderr)
        return 1
    for line in lines:
        # Diff mode reports each failing advisory once below, as FAIL or PRE-EXISTING.
        if not (base and line.endswith(": FAIL")):
            print(line)
    for finding in dict.fromkeys(errors):
        if finding in existing:
            print(f"PRE-EXISTING (also on {base}, not introduced here): {finding}")
        else:
            print(f"FAIL: {finding}", file=sys.stderr)
    if existing:
        print(
            "Pre-existing findings fail the audit on main and nightly; fix them in their own "
            f"pull request: {FIX_PROCESS}"
        )
    return int(any(finding not in existing for finding in errors))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", help="Fail only on findings absent at this git revision (pull requests)"
    )
    raise SystemExit(main(base=parser.parse_args().base))
