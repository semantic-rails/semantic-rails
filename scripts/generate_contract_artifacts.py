#!/usr/bin/env python3
"""Generate or check the code-derived public contract artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from semantic_rails.contracts import CONTRACT_NAMES  # noqa: E402
from semantic_rails.contracts.generation import (  # noqa: E402
    generated_artifacts,
    render_artifact,
)

PACKAGE_DIR = REPO_ROOT / "semantic_rails" / "contracts"
SCHEMA_DIR = REPO_ROOT / "schemas"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if committed generated artifacts differ from executable definitions.",
    )
    args = parser.parse_args(argv)
    changed: list[str] = []
    for filename, payload in generated_artifacts().items():
        path = PACKAGE_DIR / filename
        rendered = render_artifact(payload)
        if path.is_file() and path.read_text(encoding="utf-8") == rendered:
            continue
        changed.append(filename)
        if not args.check:
            path.write_text(rendered, encoding="utf-8")
    for filename in CONTRACT_NAMES:
        source = PACKAGE_DIR / filename
        destination = SCHEMA_DIR / filename
        rendered = source.read_text(encoding="utf-8")
        if destination.is_file() and destination.read_text(encoding="utf-8") == rendered:
            continue
        changed.append(f"schemas/{filename}")
        if not args.check:
            destination.write_text(rendered, encoding="utf-8")
    if args.check and changed:
        print("Generated contract artifacts are stale: " + ", ".join(changed), file=sys.stderr)
        print("Run: python scripts/generate_contract_artifacts.py", file=sys.stderr)
        return 1
    if changed:
        print("Generated contract artifacts: " + ", ".join(changed))
    else:
        print("Contract artifacts are current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
