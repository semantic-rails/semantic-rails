"""A joined call's compile grows with its parts, not faster: each part is compiled and rendered
once (a repeat of the call reads its parts from the compile cache), the joined statement is
rendered once, each key is read once per part, and each count the answer states is one term.
The one term per pair of parts is the count of the keys both have, which the pairwise counts
of ``PARTS_JOINED`` need; nothing else grows with the pairs."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from semantic_rails import compiler
from semantic_rails.cache import CachedCompilation, compilation_cache_key
from semantic_rails.embedding import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PackageConfig
from tests.semantic_rails.hidden_absent import visibility_policy

STORE = "dimension.jaffle_store_name"
NOW = "2026-10-05T06:00:00Z"


def _part(index: int) -> dict[str, Any]:
    return {
        "select": [{"expression": "measure.jaffle.order_count", "as": f"orders_{index}"}],
        "group_by": [STORE],
    }


def _call(parts: int) -> dict[str, Any]:
    return {"parts": [_part(index) for index in range(parts)], "join": {"on": [[STORE] * parts]}}


class _Recording:
    """A compile cache that keeps every entry and records the keys put."""

    def __init__(self) -> None:
        self.store: dict[str, CachedCompilation] = {}
        self.puts: list[str] = []

    def get(self, key: str) -> CachedCompilation | None:
        return self.store.get(key)

    def put(self, key: str, value: CachedCompilation) -> None:
        self.puts.append(key)
        self.store[key] = value


@pytest.fixture()
def package(package_config_factory: Callable[[str], tuple[Any, Path]]) -> tuple[Any, Path]:
    return package_config_factory("jaffle_shop")


@pytest.fixture()
def runtime(package: tuple[PackageConfig, Path]) -> Iterator[Runtime]:
    config, path = package
    made = Runtime.from_config(config, source_path=str(path))
    yield made
    made.close()


def test_the_statement_grows_with_the_parts_and_one_count_per_pair(runtime):
    """The SQL of 2 to 6 parts is a quadratic in the parts whose second difference (what one
    more pair of parts adds) is exactly one count column's lines: its definition and the two
    selects that carry it. A key read in every count, or a count beside every other count,
    would add a cubic term or a longer pair."""
    sql = {parts: runtime.compile(_call(parts), now=NOW)["rendered_sql"] for parts in range(2, 7)}
    sizes = [len(sql[parts]) for parts in range(2, 7)]
    first = [later - earlier for earlier, later in zip(sizes, sizes[1:], strict=False)]
    second = [later - earlier for earlier, later in zip(first, first[1:], strict=False)]
    third = [later - earlier for earlier, later in zip(second, second[1:], strict=False)]
    # A pair's column: its line in each select, each with its comma and its newline.
    lines = [line.rstrip(",") for line in sql[2].splitlines() if "joined_shared_0_1" in line]
    pair = sum(len(line) + 2 for line in lines)

    assert third == [0, 0]
    assert second == [pair, pair, pair]
    assert len(lines) == 3  # defined, then carried by the kept rows and the answer
    # Each key is read from the parts once: the one column the counts read.
    assert sql[6].count("COALESCE(part_0.") == 1


def test_each_part_renders_once_and_a_repeat_reads_the_parts_from_the_cache(runtime, monkeypatch):
    rendered: list[int] = []
    render = compiler.render_select_for_profile

    def counted(*args: Any, **kwargs: Any) -> str:
        rendered.append(1)
        return render(*args, **kwargs)

    monkeypatch.setattr(compiler, "render_select_for_profile", counted)
    call = _call(4)
    first = runtime.validate(call, now=NOW)
    renders_first = len(rendered)
    second = runtime.validate(call, now=NOW)

    assert first["ok"] and second["ok"]
    assert renders_first == 4 + 1  # each part's statement, then the joined one
    assert len(rendered) - renders_first == 1  # the joined statement; the parts are cached
    assert runtime.query(call, now=NOW)["rows"]
    assert len(rendered) - renders_first == 2


def test_a_parts_compile_is_never_the_calls_and_never_another_callers(package):
    """A part is cached under the call's own key fields (package, call, warehouse, profile,
    identity, roles, attribute values, the caller's hidden set) and a mark that it is a part:
    the call alone never reads a part's statement, a part never reads the call's, and a caller
    with another view never reads either."""
    config, path = package
    governed = replace(
        config,
        semantic_policies=[
            *config.semantic_policies,
            visibility_policy("hidden", "measure.jaffle.revenue_usd"),
        ],
    )
    runtime = Runtime.from_config(governed, source_path=str(path))
    cache = _Recording()
    runtime.set_compile_cache(cache)
    try:
        alone = runtime.compile(_part(0), now=NOW)["rendered_sql"]
        joined = runtime.compile(_call(2), now=NOW)["rendered_sql"]
        assert runtime.compile(_part(0), now=NOW)["rendered_sql"] == alone
        assert len(cache.puts) == 3  # the call alone, then each part as a part

        support = RequestContext(roles=("support",))
        seen = set(cache.puts)
        assert runtime.compile(_call(2), now=NOW, context=support)["rendered_sql"] == joined
        assert len(cache.puts) == 5 and not seen & set(cache.puts[3:])

        runtime.set_compile_cache(_Recording())
        assert runtime.compile(_call(2), now=NOW)["rendered_sql"] == joined
        assert runtime.compile(_part(0), now=NOW)["rendered_sql"] == alone
    finally:
        runtime.close()


def test_the_cache_key_marks_a_part():
    def key(**marked: bool) -> str:
        return compilation_cache_key(
            package_hash="package",
            normalized_query={"select": ["measure.m"], "group_by": ["dimension.d"]},
            warehouse="duckdb",
            relation_profile="default",
            policy_context={},
            aggregate_routing=False,
            **marked,
        )

    assert key() == key(joined_part=False) != key(joined_part=True)
