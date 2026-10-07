"""Deterministic whole-file partitioning for backend CI and its flake guard."""

from __future__ import annotations

import hashlib
import os

import pytest


def in_shard(nodeid: str) -> bool:
    try:
        count = int(os.environ.get("SR_SHARD_COUNT", "1"))
        index = int(os.environ.get("SR_SHARD_INDEX", "0"))
    except ValueError as exc:
        raise pytest.UsageError("Shard count and index must be integers") from exc
    if count < 1 or not 0 <= index < count:
        raise pytest.UsageError("Require SR_SHARD_COUNT > 0 and 0 <= SR_SHARD_INDEX < count")
    path = nodeid.split("::", 1)[0]
    return int.from_bytes(hashlib.sha256(path.encode()).digest(), "big") % count == index


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]):
    # Quarantine validates the complete collection before this wrapper partitions it.
    yield
    in_shard("")  # Validate configuration even for an empty collection.
    deselected = [item for item in items if not in_shard(item.nodeid)]
    items[:] = [item for item in items if in_shard(item.nodeid)]
    config.hook.pytest_deselected(items=deselected)
