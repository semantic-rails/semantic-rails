"""Empty-terms discover lists the ids an agent picks a starting point from, by model.

Each model the caller sees lists its measure, metric, segment and dimension ids; past one page of
dimensions each model counts them instead. ``limit`` and ``offset`` page the models, and a page
ends at the models that fit the response budget, so the default call fits on a large package.
``kinds`` (and a resource grant, which grants no models) list each kind's ids instead. Every id
and count is the caller's view: a hidden object lists exactly as an absent one.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.discover_listing import id_listing
from semantic_rails.mcp import SemanticLayerMCPAdapter, json_text, list_tool_definitions
from semantic_rails.request_context import RequestContext
from semantic_rails.request_payload import MAX_RESULT_CHARS_ENV
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PackageConfig
from semantic_rails.visible_view import hidden_object_ids
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.hidden_absent import (
    CALLER,
    absent,
    envelope,
    visibility_policy,
    with_policies,
)
from tests.semantic_rails.visible_view_sweep import mcp_as

KINDS = (("measure", "measures"), ("metric", "metrics"), ("segment", "segments"))
# A metric that reads its measure through a window has no root entity of its own.
WINDOWED = "metric.sales.cumulative_revenue"


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PackageConfig]:
    root = copy_package_config(tmp_path_factory.mktemp("listing"), "jaffle_shop", preseed_db=True)
    return root, load_package_config(str(root))


def _wide(config: PackageConfig, count: int = 60) -> PackageConfig:
    """``config`` with ``count`` more store dimensions: more than one page of them."""
    store = next(row for row in config.dimensions if row.id == "dimension.jaffle_store_name")
    extra = [
        replace(
            store,
            id=f"dimension.jaffle_store_note_{i:03d}",
            name=f"jaffle.Store.note_{i:03d}",
            label=f"Note {i}",
        )
        for i in range(count)
    ]
    return replace(config, dimensions=[*config.dimensions, *extra])


@pytest.fixture()
def adapter(package) -> Iterator[SemanticLayerMCPAdapter]:
    root, config = package
    adapter = SemanticLayerMCPAdapter(Runtime.from_config(config, source_path=str(root)))
    try:
        yield adapter
    finally:
        adapter.close()


def _index(adapter: SemanticLayerMCPAdapter) -> dict[str, Any]:
    return adapter.read_resource("semantic-rails://catalog/index")["payload"]["catalog"]


def _pages(adapter: SemanticLayerMCPAdapter, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Every page of an empty-terms listing, following ``next_offset``."""
    pages, offset = [], 0
    while True:
        page = adapter.call_tool("discover", {"terms": "", **arguments, "offset": offset})
        assert page["ok"] is True, page
        pages.append(page)
        cut = [w for w in page.get("warnings") or [] if w["code"] == "DISCOVER_IDS_TRUNCATED"]
        if not cut:
            return pages
        offset = cut[0]["details"]["next_offset"]


def test_empty_terms_list_each_model_with_its_ids(package, adapter) -> None:
    _, config = package
    listed = adapter.call_tool("discover", {"terms": ""})
    assert listed["ok"] is True and "warnings" not in listed
    catalog, index = listed["catalog"], _index(adapter)
    # The ids to start from, and nothing the agent reads on an id's card or the index.
    assert set(catalog) == {"counts", "description", "models"}
    assert catalog["counts"] == index["counts"]
    assert list(catalog["models"]) == index["entity_ids"]
    owners = {row.id: row.entity for row in (*config.measures, *config.segments)}
    owners |= {row.id: row.entity for row in config.dimensions}
    for kind, key in (*KINDS, ("dimension", "dimensions")):
        listed_ids = [i for model in catalog["models"].values() for i in model.get(key, [])]
        assert sorted(listed_ids) == index[f"{kind}_ids"], kind
        for model, entry in catalog["models"].items():
            for object_id in entry.get(key, []):
                assert owners.get(object_id, model) == model, object_id
    # A metric is listed under its measure's model, a windowed one too.
    revenue = next(row for row in config.measures if row.id == "measure.jaffle.revenue_usd")
    assert WINDOWED in catalog["models"][revenue.entity]["metrics"]
    # A model's label is listed when it says more than the model's id.
    labels = {m: entry["label"] for m, entry in catalog["models"].items() if "label" in entry}
    assert labels == {"entity.jaffle_time": "Calendar"}


def test_more_dimensions_than_a_page_are_counted_per_model(package) -> None:
    root, config = package
    wide = _wide(config)
    adapter = SemanticLayerMCPAdapter(Runtime.from_config(wide, source_path=str(root)))
    try:
        listed = adapter.call_tool("discover", {"terms": ""})
        assert listed["ok"] is True
        (note,) = listed["warnings"]
        dimensions = len(wide.dimensions)
        assert note == {
            "code": "DISCOVER_DIMENSIONS_COUNTED",
            "severity": "info",
            "message": (
                f"{dimensions:,} dimensions are more than one page (100), so each model gives "
                'its dimension_count. Pass kinds ["dimension"] to page their ids, or terms to '
                "rank them."
            ),
            "details": {"dimensions": dimensions},
        }
        models = listed["catalog"]["models"]
        assert all("dimensions" not in entry for entry in models.values())
        assert sum(entry.get("dimension_count", 0) for entry in models.values()) == dimensions
        assert models["entity.jaffle_store"]["dimension_count"] == sum(
            row.entity == "entity.jaffle_store" for row in wide.dimensions
        )
        # The measures, metrics and segments are still listed by id.
        assert models["entity.jaffle_order"]["measures"]
        # kinds pages the dimension ids themselves.
        pages = _pages(adapter, {"kinds": ["dimension"]})
        assert pages[0]["warnings"][0]["details"] == {
            "remaining": {"dimension": dimensions - 100},
            "next_offset": 100,
        }
        ids = [i for page in pages for i in page["catalog"]["dimension_ids"]]
        assert sorted(ids) == sorted(row.id for row in wide.dimensions)
    finally:
        adapter.close()


def test_limit_and_offset_page_the_models(adapter) -> None:
    whole = adapter.call_tool("discover", {"terms": ""})["catalog"]
    first = adapter.call_tool("discover", {"terms": "", "limit": 4})
    (warning,) = first["warnings"]
    assert warning == {
        "code": "DISCOVER_IDS_TRUNCATED",
        "severity": "warning",
        "message": (
            f"Listed 4 of {len(whole['models'])} models from offset 0. Pass offset=4 for the next "
            "page, kinds to list one kind's ids, or terms to rank candidates."
        ),
        "details": {"remaining": {"models": len(whole["models"]) - 4}, "next_offset": 4},
    }
    pages = _pages(adapter, {"limit": 4})
    assert len(pages) == -(-len(whole["models"]) // 4)
    assert all(page["catalog"]["counts"] == whole["counts"] for page in pages)
    merged = {m: e for page in pages for m, e in page["catalog"]["models"].items()}
    assert merged == whole["models"]


def test_a_page_ends_at_the_models_that_fit_the_budget(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole = adapter.call_tool("discover", {"terms": ""})
    budget = len(json_text(whole)) // 2
    monkeypatch.setenv(MAX_RESULT_CHARS_ENV, str(budget))
    pages = _pages(adapter, {})
    assert len(pages) > 1
    for page in pages:
        assert len(json_text(page)) <= budget
    merged = {m: e for page in pages for m, e in page["catalog"]["models"].items()}
    assert merged == whole["catalog"]["models"]


def test_a_hidden_object_lists_as_if_absent(package) -> None:
    root, config = package
    for package_config in (config, _wide(config)):
        governed = with_policies(
            package_config,
            visibility_policy(
                "hidden",
                "entity.jaffle_supply",
                "measure.jaffle.revenue_mtd_usd",
                "dimension.jaffle_store_note_001",
                "dimension.jaffle_customer_name",
            ),
        )
        hidden = hidden_object_ids(governed, roles=CALLER["roles"])
        assert {"entity.jaffle_supply", "dimension.jaffle_customer_name"} <= hidden
        runtime = Runtime.from_config(governed, source_path=str(root))
        without = Runtime.from_config(absent(governed, hidden), source_path=str(root))
        try:
            for arguments in ({}, {"limit": 3, "offset": 3}, {"kinds": ["dimension", "measure"]}):
                call = {"terms": "", **arguments}
                seen = mcp_as(runtime, "discover", call, CALLER)
                assert seen["ok"] is True, seen
                assert envelope(seen) == envelope(mcp_as(without, "discover", call, CALLER))
                text = json_text(seen)
                assert not [object_id for object_id in hidden if object_id in text]
        finally:
            runtime.close()
            without.close()


def test_a_grant_lists_only_its_ids_by_kind(package) -> None:
    root, config = package
    granted = ("dimension.jaffle_customer_name", "dimension.jaffle_customer_type")
    context = RequestContext(
        actor="subject",
        roles=("analyst",),
        audience="finance",
        metric_allowlist=("metric.sales.customer_count",),
        dimension_allowlist=granted,
    )
    adapter = SemanticLayerMCPAdapter(Runtime.from_config(config, source_path=str(root)))
    try:
        listed = adapter.call_tool("discover", {"terms": ""}, request_context=context)
        assert listed["ok"] is True, listed
        catalog = listed["catalog"]
        # A grant names no model, so its ids list by kind and no model's id appears.
        assert "models" not in catalog and catalog["entity_ids"] == []
        assert "entity." not in json_text(catalog)
        assert catalog["metric_ids"] == ["metric.sales.customer_count"]
        assert catalog["dimension_ids"] == sorted(granted)
        assert catalog["counts"]["metrics"] == 1 and catalog["counts"]["dimensions"] == 2
        assert catalog["counts"]["measures"] == 0
    finally:
        adapter.close()


def test_an_id_whose_model_the_caller_cant_see_lists_by_kind(package, adapter) -> None:
    _, config = package
    index = _index(adapter)
    # A catalog that lists the order model's measures but not the model.
    unseen = {**index, "entity_ids": [i for i in index["entity_ids"] if i != "entity.jaffle_order"]}
    page, _ = id_listing(unseen, config, kinds=[], offset=0, limit=100, max_chars=30_000)
    orders = sorted(row.id for row in config.measures if row.entity == "entity.jaffle_order")
    assert "entity.jaffle_order" not in page["models"]
    assert orders and set(orders) <= set(page["measure_ids"])


def test_the_limit_argument_says_has_more_counts_the_rest() -> None:
    (discover,) = [tool for tool in list_tool_definitions() if tool["name"] == "discover"]
    limit = discover["inputSchema"]["properties"]["limit"]["description"]
    assert "has_more counts the rest" in limit
