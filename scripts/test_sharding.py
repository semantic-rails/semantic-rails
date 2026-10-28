"""Deterministic cost-balanced whole-file partitioning for CI and its flake guard.

A pull request also runs one sixth of the visible-view sweep (``SWEEP_FILES``): set
``SR_SWEEP_SEED`` (CI passes the pull request's head commit) and only the parametrized
cases whose node ID hashes to that seed's slice run there. Each push picks a slice from
its own commit, so a pull request's pushes cover different sixths; the merge queue and
every other run leave the seed unset and run every case.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import zlib
from functools import cache
from pathlib import Path
from statistics import median

import pytest

ROOT = Path(__file__).resolve().parents[1]
DURATIONS = ROOT / "tests/shard_durations.json"
# The visible-view sweep and the hidden-as-unknown matrix: about half the suite's summed
# test time, in parametrized cases that each check one object or one request shape.
SWEEP_FILES = frozenset(
    {
        "tests/semantic_rails/test_hidden_as_unknown.py",
        *(f"tests/semantic_rails/test_visible_view_sweep_{index}.py" for index in range(6)),
    }
)
SWEEP_SLICES = 6


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
def shard_owners(root: Path, count: int, sliced: bool = False) -> dict[str, int]:
    durations = json.loads(DURATIONS.read_text(encoding="utf-8"))
    if sliced:  # A sweep file costs a sixth of its full run.
        durations = {
            file: cost / SWEEP_SLICES if file in SWEEP_FILES else cost
            for file, cost in durations.items()
        }
    return assign_files(test_files(root), durations, count)


def sweep_slice() -> int | None:
    """The sweep slice this run takes, or None to run every sweep case."""
    seed = os.environ.get("SR_SWEEP_SEED", "")
    if not seed:
        return None
    return int(hashlib.sha256(seed.encode()).hexdigest(), 16) % SWEEP_SLICES


def in_sweep_slice(nodeid: str, root: Path = ROOT) -> bool:
    """Whether this run takes the test: every test outside the sweep's parametrized cases,
    and those cases whose node ID hashes to this run's slice."""
    chosen = sweep_slice()
    path, _, name = nodeid.partition("::")
    if chosen is None or not name.endswith("]"):
        return True
    if Path(path).is_absolute():
        path = Path(path).relative_to(root.resolve()).as_posix()
    return path not in SWEEP_FILES or zlib.crc32(nodeid.encode()) % SWEEP_SLICES == chosen


def _shard() -> tuple[int, int]:
    try:
        count = int(os.environ.get("SR_SHARD_COUNT", "1"))
        index = int(os.environ.get("SR_SHARD_INDEX", "0"))
    except ValueError as exc:
        raise pytest.UsageError("Shard count and index must be integers") from exc
    if count < 1 or not 0 <= index < count:
        raise pytest.UsageError("Require SR_SHARD_COUNT > 0 and 0 <= SR_SHARD_INDEX < count")
    return count, index


def in_shard(nodeid: str, root: Path = ROOT) -> bool:
    """Whether this shard owns the test's file; one outside the inventory (a deleted file
    named by a quarantine entry) belongs to shard zero, so exactly one shard reports it."""
    count, index = _shard()
    if count == 1 or not nodeid:
        return True
    root = root.resolve()
    path = nodeid.split("::", 1)[0]
    if Path(path).is_absolute():
        path = Path(path).relative_to(root).as_posix()
    return shard_owners(root, count, sweep_slice() is not None).get(path, 0) == index


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Never import a test file another shard owns: every worker of every shard collected
    the whole suite, about half a minute per backend job, to deselect most of it."""
    count, index = _shard()
    if count == 1 or not collection_path.name.startswith("test_"):
        return None
    root = config.rootpath.resolve()
    try:
        path = collection_path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None
    owner = shard_owners(root, count, sweep_slice() is not None).get(path)
    return True if owner is not None and owner != index else None


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]):
    # Quarantine validates this shard's entries before this wrapper partitions the items.
    yield
    in_shard("")  # Validate configuration even for an empty collection.
    root = config.rootpath
    keep = [in_shard(item.nodeid, root) and in_sweep_slice(item.nodeid, root) for item in items]
    deselected = [item for item, kept in zip(items, keep, strict=True) if not kept]
    items[:] = [item for item, kept in zip(items, keep, strict=True) if kept]
    config.hook.pytest_deselected(items=deselected)


def pytest_report_header(config: pytest.Config) -> str | None:
    chosen = sweep_slice()
    if chosen is None:
        return None
    seed = os.environ["SR_SWEEP_SEED"]
    return f"visible-view sweep: slice {chosen} of {SWEEP_SLICES} (SR_SWEEP_SEED={seed})"
