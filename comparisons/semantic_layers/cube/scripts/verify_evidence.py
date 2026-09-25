"""Check the Cube install surface offline (no npm): pinned manifest, registry lock, clean audit."""

from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
REGISTRY = "https://registry.npmjs.org/"
EXACT = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


def _load(name: str) -> dict:
    return json.loads((PROJECT_DIR / name).read_text(encoding="utf-8"))


def _dependencies(manifest: dict) -> dict[str, str]:
    fields = ("dependencies", "devDependencies")
    return {f"{f} {name}": v for f in fields for name, v in manifest.get(f, {}).items()}


def errors() -> list[str]:
    package, lock = _load("package.json"), _load("package-lock.json")
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
    counts = _load("npm-audit.json").get("metadata", {}).get("vulnerabilities", {})
    for severity in ("high", "critical"):
        if counts.get(severity) != 0:
            found.append(f"npm-audit.json reports {counts.get(severity)} {severity} advisories")
    return found


def main() -> int:
    found = errors()
    for error in found:
        print(f"- {error}")
    print("Cube install surface failed the offline check." if found else "Cube install surface OK.")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
