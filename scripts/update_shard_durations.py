"""Regenerate per-file CI costs from downloaded backend JUnit reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median

try:
    from scripts.flake_guard import measured_durations
    from scripts.test_sharding import DURATIONS, ROOT, assign_files, test_files
except ModuleNotFoundError:  # Direct script invocation.
    from flake_guard import measured_durations
    from test_sharding import DURATIONS, ROOT, assign_files, test_files


def duration_table(reports: list[Path], files: list[str]) -> dict[str, float]:
    """Use the slowest observed total per file across Python versions and runs."""
    costs: dict[str, float] = {}
    for report in reports:
        measured = measured_durations(report, files)
        if not measured:
            raise ValueError(f"No usable test durations in {report}")
        for file, duration in measured.items():
            costs[file] = max(costs.get(file, 0), max(0.001, round(duration, 3)))
    if not costs:
        raise ValueError("At least one JUnit report is required")
    return dict(sorted(costs.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=DURATIONS)
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--target-seconds", type=float, default=720)
    args = parser.parse_args()
    if args.shards < 1 or args.workers < 1 or args.target_seconds <= 0:
        parser.error("Shards, workers and target seconds must be positive")
    files = test_files(ROOT)
    costs = duration_table(args.reports, files)
    owners = assign_files(files, costs, args.shards)
    fallback = median(costs.values())
    for file in files:
        cost = costs.get(file, fallback)
        if cost / args.workers > args.target_seconds:
            print(f"WARNING: {file} alone exceeds target; split this file by hand")
    for index in range(args.shards):
        total = sum(costs.get(file, fallback) for file in files if owners[file] == index)
        print(f"Shard {index}: summed cost {total:.1f}s; estimate {total / args.workers:.1f}s")
    print(f"Measured {len(costs)}/{len(files)} files; unknown files use median {fallback:.3f}s")
    args.output.write_text(json.dumps(costs, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
