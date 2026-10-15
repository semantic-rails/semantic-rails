from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from semantic_rails.cache import (
    CachedCompilation,
    CompiledSqlCache,
    LruCompiledSqlCache,
    compilation_cache_key,
)
from semantic_rails.request_context import RequestContext, TrustedAttributes


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
    """A minimal `CompiledSqlCache` that records the keys it is asked for."""

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


def test_runtime_set_compile_cache_takes_only_a_protocol_implementation(runtime_factory):
    """`Runtime.set_compile_cache(cache)` replaces the in-process LRU with a process-local
    backend that implements the runtime-checkable `CompiledSqlCache` protocol, and keeps it
    across a reload. An object missing `get` or `put` fails at injection time, not at the first
    cached query."""

    class _MissingGet:
        def put(self, key, value):  # pragma: no cover - signature only
            return None

    class _MissingPut:
        def get(self, key):  # pragma: no cover - signature only
            return None

    assert isinstance(LruCompiledSqlCache(), CompiledSqlCache)
    assert not isinstance(object(), CompiledSqlCache)
    runtime = runtime_factory("jaffle_shop")
    try:
        for wrong in (_MissingGet(), _MissingPut(), "not-a-cache"):
            with pytest.raises(TypeError, match="CompiledSqlCache"):
                runtime.set_compile_cache(wrong)  # type: ignore[arg-type]
        injected = _RecordingCache()
        assert isinstance(injected, CompiledSqlCache)
        runtime.set_compile_cache(injected)
        assert runtime._compile_cache is injected
        runtime._compile_cache.put("test-key", CachedCompilation(compiled={"sql": "select 1"}))
        assert injected.put_calls == ["test-key"]
        runtime.reload()
        assert runtime._compile_cache is injected
    finally:
        runtime.close()


def _key(policy_context):
    return compilation_cache_key(
        package_hash="package",
        normalized_query={"select": ["metric.m"]},
        warehouse="duckdb",
        relation_profile="default",
        policy_context=policy_context,
        aggregate_routing=False,
    )


def _host_context(**attributes):
    return RequestContext(actor="host-user", attributes=attributes).to_policy_context()


def test_compile_cache_key_is_partitioned_by_trusted_attribute_values():
    # The attributes' repr names keys only; the key must still see each typed value.
    variants = [{}, {"customer_id": "1"}, {"customer_id": "2"}, {"customer_id": 1}]
    variants += [{"customer_id": True}, {"customer_id": ["1"]}, {"account_id": "1"}]
    # A list's order and length compile apart: each binds its own slots.
    variants += [{"customer_id": ["1", "2"]}, {"customer_id": ["2", "1"]}, {"customer_id": [1]}]
    variants += [{"customer_id": []}]
    keys = [_key(_host_context(**attributes)) for attributes in variants]
    assert len(set(keys)) == len(variants)
    # Verified roles decide row-filter exemptions, so they partition the key too.
    verified = RequestContext(attributes=TrustedAttributes({}, roles=["auditor"]))
    assert _key(verified.to_policy_context()) != _key(_host_context())
    assert _key(_host_context(a="x", b=2)) == _key(_host_context(b=2, a="x"))
    # Caller JSON never chooses a partition; it is dropped like any untrusted attribute.
    spoofed = {**_host_context(), "attributes": {"customer_id": "1"}}
    assert _key(spoofed) == _key(_host_context())


def test_runtime_compile_cache_never_serves_one_attribute_value_to_another(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    recording = _RecordingCache()
    runtime.set_compile_cache(recording)
    request = {"select": ["metric.sales.aov_usd"]}
    try:
        compiled = [
            runtime.compile(
                request,
                context=RequestContext(actor="host-user", attributes={"customer_id": customer}),
            )
            for customer in ("c-a", "c-b", "c-a", "c-b")
        ]
        key_a, key_b = recording.get_calls[:2]
        assert key_a != key_b
        assert recording.get_calls == [key_a, key_b, key_a, key_b]
        assert recording.put_calls == [
            key_a,
            key_b,
        ]  # the repeats were hits, in their own partition
        assert len({result["rendered_sql"] for result in compiled}) == 1
    finally:
        runtime.close()
