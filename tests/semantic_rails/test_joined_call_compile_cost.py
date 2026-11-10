"""A joined call's compile grows with its parts, not faster: each part is compiled and rendered
once, the joined statement is rendered once, each key is read once per part, and each count the
answer states is one term. The one term per pair of parts is the count of the keys both have,
which the pairwise counts of ``PARTS_JOINED`` need; nothing else grows with the pairs."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from semantic_rails import compiler
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PackageConfig

STORE = "dimension.jaffle_store_name"
NOW = "2026-10-05T06:00:00Z"


def _part(index: int) -> dict[str, Any]:
    return {
        "select": [{"expression": "measure.jaffle.order_count", "as": f"orders_{index}"}],
        "group_by": [STORE],
    }


def _call(parts: int) -> dict[str, Any]:
    return {"parts": [_part(index) for index in range(parts)], "join": {"on": [[STORE] * parts]}}


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


def test_each_part_renders_once_and_the_joined_statement_once(runtime, monkeypatch):
    rendered: list[int] = []
    render = compiler.render_select_for_profile

    def counted(*args: Any, **kwargs: Any) -> str:
        rendered.append(1)
        return render(*args, **kwargs)

    monkeypatch.setattr(compiler, "render_select_for_profile", counted)
    for parts in (2, 4, 8):
        rendered.clear()
        assert runtime.validate(_call(parts), now=NOW)["ok"]
        assert len(rendered) == parts + 1  # each part's own statement, then the joined one
    rendered.clear()
    assert runtime.query(_call(8), now=NOW)["rows"]
    assert len(rendered) == 8 + 1
