from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from semantic_rails.cache import CachedCompilation, CompiledSqlCache, LruCompiledSqlCache


def test_lru_compiled_sql_cache_returns_defensive_copies():
    cache = LruCompiledSqlCache(maxsize=2)
    cache.put(
        "query", CachedCompilation(compiled={"sql": "select 1", "nested": {"items": ["original"]}})
    )

    first = cache.get("query")
    assert first is not None
    first.compiled["sql"] = "select broken"
    first.compiled["nested"]["items"].append("mutated")

    second = cache.get("query")
    assert second is not None
    assert second.compiled == {"sql": "select 1", "nested": {"items": ["original"]}}


def test_lru_compiled_sql_cache_supports_concurrent_get_and_put():
    cache = LruCompiledSqlCache(maxsize=8)

    def worker(index: int) -> str:
        key = f"query-{index % 4}"
        cache.put(key, CachedCompilation(compiled={"index": index, "nested": {"values": [index]}}))
        cached = cache.get(key)
        assert cached is not None
        cached.compiled["nested"]["values"].append("local")
        return key

    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(worker, range(64)))

    assert set(keys) == {"query-0", "query-1", "query-2", "query-3"}


class _RecordingCache:
    """Minimal `CompiledSqlCache` implementation used to confirm
    `Runtime.set_compile_cache` accepts a protocol-conforming injection
    and routes subsequent `get`/`put` traffic to it."""

    def __init__(self) -> None:
        self.store: dict[str, CachedCompilation] = {}
        self.get_calls: list[str] = []
        self.put_calls: list[str] = []

    def get(self, key: str) -> CachedCompilation | None:
        self.get_calls.append(key)
        return self.store.get(key)

    def put(self, key: str, value: CachedCompilation) -> None:
        self.put_calls.append(key)
        self.store[key] = value


def test_runtime_set_compile_cache_accepts_protocol_implementation(runtime_factory):
    """Public extension seam: `Runtime.set_compile_cache(cache)` replaces
    the in-process LRU with a protocol-conforming process-local backend.
    The underscore-prefixed attribute stays internal; the setter is the
    typed-object contract, not a distributed serialization promise.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        injected = _RecordingCache()
        runtime.set_compile_cache(injected)
        assert runtime._compile_cache is injected, (
            "setter must swap the internal cache to the injected instance"
        )
        # Smoke: the runtime can now write into the new backend.
        runtime._compile_cache.put("test-key", CachedCompilation(compiled={"sql": "select 1"}))
        assert injected.put_calls == ["test-key"]
    finally:
        runtime.close()


def test_runtime_reload_preserves_injected_compile_cache(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        injected = _RecordingCache()
        runtime.set_compile_cache(injected)
        runtime.reload()
        assert runtime._compile_cache is injected
    finally:
        runtime.close()


def test_runtime_set_compile_cache_rejects_non_conforming_object(runtime_factory):
    """A misconfigured deployment that injects an object missing `get`/`put`
    must fail fast at injection time, not at the first cached-query
    request. Otherwise the cache becomes a silent foot-gun.
    """

    class _MissingGet:
        def put(self, key, value):  # pragma: no cover - signature only
            return None

    class _MissingPut:
        def get(self, key):  # pragma: no cover - signature only
            return None

    runtime = runtime_factory("jaffle_shop")
    try:
        with pytest.raises(TypeError, match="CompiledSqlCache"):
            runtime.set_compile_cache(_MissingGet())  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="CompiledSqlCache"):
            runtime.set_compile_cache(_MissingPut())  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="CompiledSqlCache"):
            runtime.set_compile_cache("not-a-cache")  # type: ignore[arg-type]
    finally:
        runtime.close()


def test_compiled_sql_cache_protocol_runtime_checkable():
    """The protocol must be `@runtime_checkable` so isinstance() works at
    the setter boundary — that's the only way the setter can validate
    inputs at startup without forcing operators to subclass anything."""
    assert isinstance(LruCompiledSqlCache(), CompiledSqlCache)
    assert isinstance(_RecordingCache(), CompiledSqlCache)
    assert not isinstance(object(), CompiledSqlCache)
