"""Compilation caches and package-fingerprint helpers.

Exposes :class:`CompiledSqlCache` (the pluggable process-local protocol),
:class:`LruCompiledSqlCache`
(the single-process default), and :func:`compilation_cache_key` /
:func:`package_fingerprint` used to key compiled output by both the
query shape and the package version.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any, Protocol, runtime_checkable

COMPILER_CACHE_VERSION = "semantic-rails-compiler-v2-physical-plan-1"


@dataclass
class CachedCompilation:
    compiled: dict[str, Any]


@runtime_checkable
class CompiledSqlCache(Protocol):
    """Pluggable contract for compiled-SQL caches.

    This contract intentionally exchanges Python ``CachedCompilation``
    objects. It supports alternate process-local eviction/instrumentation
    backends injected with ``Runtime.set_compile_cache(cache)``; it is not a
    wire-format or distributed-cache contract. Compiled plans contain typed
    dataclasses and their representation can change between compiler
    versions. A shared Redis/Memcached backend must therefore wait for a
    separately versioned ``CompiledArtifact`` codec instead of serializing
    these values with ``default=str`` or pickle.

    Implementations must:

    1. Return deep-isolated values from `get` — the runtime mutates the
       returned compilation. Returning a shared reference will corrupt
       the cache after the first call.
    2. Be thread-safe. The runtime calls `get` and `put` from concurrent
       request threads (or async tasks delegating to a thread pool).
    3. Preserve the exact in-process typed plan shape. Values are neither
       promised to be JSON serialisable nor compatible across releases.

    Eviction policy is implementation-defined; the default uses LRU.
    """

    def get(self, key: str) -> CachedCompilation | None: ...

    def put(self, key: str, value: CachedCompilation) -> None: ...


class LruCompiledSqlCache:
    """In-process LRU cache. Default implementation of `CompiledSqlCache`.

    Sufficient for local dev, single-process deployments, and the OSS
    standalone experience. The runtime accepts alternate process-local
    implementations for instrumentation or eviction policy; cross-process
    storage is deliberately out of contract until a versioned artifact codec
    exists.
    """

    def __init__(self, maxsize: int = 512) -> None:
        self.maxsize = max(1, int(maxsize))
        self._items: OrderedDict[str, CachedCompilation] = OrderedDict()
        self._lock = RLock()

    def get(self, key: str) -> CachedCompilation | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            self._items.move_to_end(key)
            return deepcopy(item)

    def put(self, key: str, value: CachedCompilation) -> None:
        with self._lock:
            self._items[key] = deepcopy(value)
            self._items.move_to_end(key)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)


def stable_json_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def package_fingerprint(path: str) -> str:
    root = os.path.abspath(path)
    hasher = hashlib.sha256()
    if os.path.isfile(root):
        candidates = [root]
        base_dir = os.path.dirname(root)
    else:
        base_dir = root
        candidates = []
        for current_root, dirs, files in os.walk(root):
            dirs[:] = [
                item
                for item in dirs
                if item not in {".git", ".pytest_cache", ".uv-cache", "__pycache__", ".compiled"}
            ]
            for filename in files:
                if filename.endswith((".yml", ".yaml", ".json", ".toml")):
                    candidates.append(os.path.join(current_root, filename))
    for filename in sorted(candidates):
        relpath = os.path.relpath(filename, base_dir)
        hasher.update(relpath.encode("utf-8"))
        try:
            with open(filename, "rb") as handle:
                hasher.update(handle.read())
        except FileNotFoundError:
            continue
    return hasher.hexdigest()


def compilation_cache_key(
    *,
    package_hash: str,
    normalized_query: dict[str, Any],
    warehouse: str,
    relation_profile: str,
    render_profile: str = "audit",
    policy_context: dict[str, Any],
) -> str:
    return stable_json_hash(
        {
            "compiler_version": COMPILER_CACHE_VERSION,
            "package_hash": package_hash,
            "normalized_query": normalized_query,
            "warehouse": warehouse,
            "relation_profile": relation_profile,
            "render_profile": render_profile,
            "policy_context": policy_context,
        }
    )
