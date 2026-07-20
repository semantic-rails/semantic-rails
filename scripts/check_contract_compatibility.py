#!/usr/bin/env python3
"""Fail when the current public contract bundle breaks a released baseline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from semantic_rails.contracts.compatibility import (  # noqa: E402
    compare_contract_bundles,
    load_contract_directory,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        required=True,
        type=Path,
        help="Directory containing the released baseline JSON artifacts.",
    )
    parser.add_argument(
        "--current",
        type=Path,
        default=REPO_ROOT / "semantic_rails" / "contracts",
        help="Current contract directory (defaults to the packaged source bundle).",
    )
    args = parser.parse_args(argv)
    report = compare_contract_bundles(
        load_contract_directory(args.baseline),
        load_contract_directory(args.current),
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
