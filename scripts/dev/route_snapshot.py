"""Snapshot how packages resolve every entity pair, and compare two snapshots.

``dump`` records, for each package, every route-census pair's resolution (the rung that chose
its route and the route, or the code it is refused with) and the parse report's route counts:
pairs without a recorded route, how many the ``ROUTES_UNDECIDED`` warning counts, pass-through
routes, ``ROUTE_PASS_THROUGH`` warnings and the parse time. ``compare`` prints the counts side
by side and exits 1 when a pair answered before is refused or answered by another route: a
route-ladder change may only turn a refusal into an answer, so no number moves.

Usage:
    uv run python scripts/dev/route_snapshot.py dump PACKAGE... > before.json
    uv run python scripts/dev/route_snapshot.py compare before.json after.json

``python -m tests.semantic_rails.campus_package DIR`` writes the large synthetic package.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from semantic_rails.config import load_package_config
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.route_census import census_pairs, resolve_pairs


def dump(paths: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in paths:
        started = time.perf_counter()
        report, _ = parse_config_report(PackageReference(source_path=path))
        seconds = round(time.perf_counter() - started, 1)
        # A package the parse report refuses (a stricter check than loading) has no counts.
        config = load_package_config(path)
        census = report.get("route_census") or {}
        warnings = [row for row in report["warnings"] if isinstance(row, dict)]
        warned = [row for row in warnings if row["code"] == "ROUTES_UNDECIDED"]
        out[path] = {
            "counts": {
                "pairs": len(census_pairs(config)),
                "undecided": len(census.get("undecided", [])),
                "routes_undecided_warning": warned[0]["details"]["count"] if warned else 0,
                "pass_through": len(census.get("pass_through", [])),
                "pass_through_warnings": sum(
                    row["code"] == "ROUTE_PASS_THROUGH" for row in warnings
                ),
                "parse_seconds": seconds,
            }
            if report["ok"]
            else {"parse_errors": len(report["errors"])},
            "pairs": {
                f"{start} -> {target}": f"refused {outcome.refused}"
                if outcome.refused
                else f"{outcome.basis}: {', '.join(outcome.path)}"
                for (start, target), outcome in resolve_pairs(config, census_pairs(config)).items()
            },
        }
    return out


def compare(before: dict[str, Any], after: dict[str, Any]) -> int:
    moved = 0
    for path in sorted(set(before) & set(after)):
        old, new = before[path], after[path]
        print(path)
        for name, value in old["counts"].items():
            print(f"  {name}: {value} -> {new['counts'].get(name)}")
        answered = 0
        for pair, was in old["pairs"].items():
            now = new["pairs"].get(pair, "missing")
            if was == now:
                continue
            if was.startswith("refused") and not now.startswith("refused"):
                answered += 1
                continue
            moved += not was.startswith("refused")
            print(
                f"  {'MOVED' if not was.startswith('refused') else 'changed'} {pair}: {was} -> {now}"
            )
        print(f"  newly answered: {answered}")
    print("answered pairs moved:", moved)
    return 1 if moved else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["dump"] and len(sys.argv) > 2:
        json.dump(dump(sys.argv[2:]), sys.stdout, indent=1, sort_keys=True)
    elif sys.argv[1:2] == ["compare"] and len(sys.argv) == 4:
        files = [json.loads(Path(name).read_text(encoding="utf-8")) for name in sys.argv[2:]]
        sys.exit(compare(*files))
    else:
        sys.exit(__doc__)
