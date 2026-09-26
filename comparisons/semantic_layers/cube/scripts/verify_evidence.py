"""Check the Cube install surface offline (no npm): pinned manifest, registry lock, clean audit,
and an index.js that keeps Cube's dev server off.

`--record` re-runs `npm audit` on the lockfile (network) and records it with the lockfile's hash.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
REGISTRY = "https://registry.npmjs.org/"
EXACT = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


def _load(project: Path, name: str) -> dict:
    return json.loads((project / name).read_text(encoding="utf-8"))


def _lock_sha256(project: Path) -> str:
    return hashlib.sha256((project / "package-lock.json").read_bytes()).hexdigest()


def _dependencies(manifest: dict) -> dict[str, str]:
    fields = ("dependencies", "devDependencies")
    return {f"{f} {name}": v for f in fields for name, v in manifest.get(f, {}).items()}


def _server_errors(project: Path) -> list[str]:
    """A tripwire, not a parser: index.js's code (comment lines aside) keeps `devServer: false`,
    checks for a .env file before loading Cube's server, and refuses CUBEJS_DEV_MODE after it."""
    lines = (project / "index.js").read_text(encoding="utf-8").splitlines()
    source = "\n".join(line for line in lines if not line.lstrip().startswith("//"))
    load = source.find('require("@cubejs-backend/server")')
    dotenv = source.find('fs.existsSync(path.join(process.cwd(), ".env"))')
    dev_mode = source.find("process.env.CUBEJS_DEV_MODE !== undefined")
    found = [] if "devServer: false," in source else ["index.js doesn't pass devServer: false"]
    if not -1 < dotenv < load:
        found.append("index.js doesn't refuse a .env file before loading @cubejs-backend/server")
    if not -1 < load < dev_mode:
        found.append("index.js doesn't refuse CUBEJS_DEV_MODE after loading @cubejs-backend/server")
    return found


def errors(project: Path = PROJECT_DIR) -> list[str]:
    package, lock = _load(project, "package.json"), _load(project, "package-lock.json")
    found = [] if package.get("private") is True else ["package.json must be private"]
    pinned = _dependencies(package)
    found += [
        f"package.json {k} isn't an exact version: {v}"
        for k, v in pinned.items()
        if not EXACT.match(v)
    ]
    if _dependencies(lock.get("packages", {}).get("", {})) != pinned:
        found.append("package-lock.json's root dependencies differ from package.json's")
    packages = {path: meta for path, meta in lock.get("packages", {}).items() if path}
    if not packages:
        found.append("package-lock.json locks no packages")
    for path, meta in sorted(packages.items()):
        if not str(meta.get("resolved", "")).startswith(REGISTRY):
            found.append(f"{path} doesn't resolve from {REGISTRY}")
        if not str(meta.get("integrity", "")).startswith("sha512-"):
            found.append(f"{path} has no sha512 integrity hash")
    audit = _load(project, "npm-audit.json")
    if audit.get("package_lock_sha256") != _lock_sha256(project):
        found.append("npm-audit.json audits another package-lock.json; re-run with --record")
    counts = audit.get("report", {}).get("metadata", {}).get("vulnerabilities", {})
    for severity in ("high", "critical"):
        if counts.get(severity) != 0:
            found.append(f"npm-audit.json reports {counts.get(severity)} {severity} advisories")
    return found + _server_errors(project)


def record(project: Path = PROJECT_DIR) -> None:
    # npm audit exits non-zero when it finds any advisory; the report is still complete.
    audit = subprocess.run(
        ["npm", "audit", "--json", "--package-lock-only"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    report = json.loads(audit.stdout or "{}")  # non-JSON output raises before anything is written
    if not isinstance(report.get("metadata", {}).get("vulnerabilities"), dict):
        raise SystemExit(f"npm audit (exit {audit.returncode}) gave no report; kept npm-audit.json")
    payload = {"package_lock_sha256": _lock_sha256(project), "report": report}
    (project / "npm-audit.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    if sys.argv[1:] == ["--record"]:
        record()
    found = errors()
    for error in found:
        print(f"- {error}")
    print("Cube install surface failed the offline check." if found else "Cube install surface OK.")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
