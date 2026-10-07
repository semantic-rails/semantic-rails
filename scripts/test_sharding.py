"""Deterministic cost-balanced whole-file partitioning for CI and its flake guard."""

from __future__ import annotations

import json
import math
import os
from functools import cache
from pathlib import Path
from statistics import median

import pytest

ROOT = Path(__file__).resolve().parents[1]
DURATIONS = ROOT / "tests/shard_durations.json"


def assign_files(files: list[str], durations: dict[str, float], count: int) -> dict[str, int]:
    """Longest first into the lightest shard; path and index break ties."""
    if count < 1:
        raise ValueError("Shard count must be positive")
    if any(not math.isfinite(cost) or cost <= 0 for cost in durations.values()):
        raise ValueError("File durations must be finite and positive")
    fallback = median(durations.values()) if durations else 1.0
    costs = {file: durations.get(file, fallback) for file in set(files)}
    loads = [0.0] * count
    owners = {}
    for file in sorted(costs, key=lambda file: (-costs[file], file)):
        index = min(range(count), key=lambda index: (loads[index], index))
        owners[file] = index
        loads[index] += costs[file]
    return owners


def test_files(root: Path) -> list[str]:
    # Discover the full universe, independent of pytest's selected files or -k.
    return sorted(
        path.relative_to(root).as_posix()
        for path in [*(root / "tests").rglob("test_*.py"), *root.glob("test_*.py")]
    )


@cache
def shard_owners(root: Path, count: int) -> dict[str, int]:
    durations = json.loads(DURATIONS.read_text(encoding="utf-8"))
    return assign_files(test_files(root), durations, count)


def in_shard(nodeid: str, root: Path = ROOT) -> bool:
    try:
        count = int(os.environ.get("SR_SHARD_COUNT", "1"))
        index = int(os.environ.get("SR_SHARD_INDEX", "0"))
    except ValueError as exc:
        raise pytest.UsageError("Shard count and index must be integers") from exc
    if count < 1 or not 0 <= index < count:
        raise pytest.UsageError("Require SR_SHARD_COUNT > 0 and 0 <= SR_SHARD_INDEX < count")
    if count == 1 or not nodeid:
        return True
    root = root.resolve()
    path = nodeid.split("::", 1)[0]
    if Path(path).is_absolute():
        path = Path(path).relative_to(root).as_posix()
    return shard_owners(root, count)[path] == index


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]):
    # Quarantine validates the complete collection before this wrapper partitions it.
    yield
    in_shard("")  # Validate configuration even for an empty collection.
    deselected = [item for item in items if not in_shard(item.nodeid, config.rootpath)]
    items[:] = [item for item in items if in_shard(item.nodeid, config.rootpath)]
    config.hook.pytest_deselected(items=deselected)
