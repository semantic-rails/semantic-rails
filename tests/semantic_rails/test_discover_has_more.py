"""A discover list cut at its limit says so: ``has_more`` names each cut bucket with how many more
matches it has. It counts only what the caller may see, so it is never an existence oracle."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import discover_payload
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PackageConfig
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.hidden_absent import CALLER, ELIGIBLE, visibility_policy, with_policies
from tests.semantic_rails.test_runtime_and_metadata import jaffle_snapshot as jaffle_snapshot
from tests.semantic_rails.visible_view_sweep import mcp_as

BUCKETS = ("measures", "metrics", "segments", "dimensions", "dimension_values", "entities")
REVENUE = "measure.jaffle.revenue_usd"
# Three of the revenue measures a "revenue" search ranks.
HIDDEN = (
    "measure.jaffle.revenue_mtd_usd",
    "measure.jaffle.revenue_qtd_usd",
    "measure.jaffle.revenue_ytd_usd",
)


@pytest.fixture()
def runtime(jaffle_snapshot) -> Iterator[Runtime]:
    runtime = Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop")
    try:
        yield runtime
    finally:
        runtime.close()


def _matches(runtime: Runtime, terms: str) -> dict[str, int]:
    """How many objects of each bucket match ``terms``, from one uncut search."""
    found = discover_payload(runtime, terms=terms, limit=10_000, enforce_scope=True)
    assert "has_more" not in found
    return {bucket: len(found[bucket]) for bucket in BUCKETS}


def _discover(runtime: Runtime, surface: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if surface.startswith("mcp"):
        verbosity = surface.removeprefix("mcp ")
        return SemanticLayerMCPAdapter(runtime).call_tool(
            "discover", {**arguments, "verbosity": verbosity}
        )
    if surface == "http":
        response, status = SemanticHTTPService(runtime).handle(
            "POST", "/discover", arguments, context=RequestContext()
        )
        assert status == 200, response
        return response
    return discover_payload(runtime, **arguments, verbosity="minimal", enforce_scope=True)


@pytest.mark.parametrize("surface", ["mcp minimal", "mcp compact", "mcp full", "http", "direct"])
def test_a_list_cut_at_its_limit_says_how_many_more_match(runtime: Runtime, surface: str) -> None:
    matches = _matches(runtime, "revenue")
    # The metrics list ends exactly at the limit; the measures list runs past it.
    limit = matches["metrics"]
    assert matches["measures"] > limit
    cut = _discover(runtime, surface, {"terms": "revenue", "limit": limit})
    assert cut["has_more"] == {"measures": matches["measures"] - limit}
    assert len(cut["metrics"]) == limit
    whole = _discover(runtime, surface, {"terms": "revenue", "limit": max(matches.values())})
    assert "has_more" not in whole


def test_compact_names_no_cut_blocked_bucket(runtime: Runtime) -> None:
    # Unavailable beside revenue, the inventory objects fill the blocked bucket past the limit
    # while each kind's list ends within it.
    adapter = SemanticLayerMCPAdapter(runtime)
    arguments = {"terms": "inventory", "query": {"select": [{"expression": REVENUE}]}}
    whole = adapter.call_tool("discover", {**arguments, "limit": 100, "verbosity": "full"})
    limit = max(len(whole[bucket]) for bucket in BUCKETS)
    assert len(whole["blocked"]) > limit
    arguments["limit"] = limit
    minimal = adapter.call_tool("discover", arguments)
    assert minimal["has_more"] == {"blocked": len(whole["blocked"]) - limit}
    # Compact leaves the blocked bucket out, so nothing it lists was cut.
    compact = adapter.call_tool("discover", {**arguments, "verbosity": "compact"})
    assert "blocked" not in compact and "has_more" not in compact


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PackageConfig]:
    root = copy_package_config(tmp_path_factory.mktemp("has_more"), "jaffle_shop", preseed_db=True)
    return root, load_package_config(str(root))


def test_an_object_hidden_from_the_caller_never_counts(package) -> None:
    root, config = package
    governed = with_policies(config, visibility_policy("hidden", *HIDDEN))
    runtime = Runtime.from_config(governed, source_path=str(root))
    try:
        terms = {"terms": "revenue", "kinds": ["measure"], "verbosity": "full"}
        seen = len(mcp_as(runtime, "discover", {**terms, "limit": 100}, CALLER)["measures"])
        everyone = len(mcp_as(runtime, "discover", {**terms, "limit": 100}, ELIGIBLE)["measures"])
        assert seen == everyone - len(HIDDEN)
        # At the limit of what the caller sees, nothing more exists for them.
        hidden_from = mcp_as(runtime, "discover", {**terms, "limit": seen}, CALLER)
        assert len(hidden_from["measures"]) == seen
        assert "has_more" not in hidden_from
        eligible = mcp_as(runtime, "discover", {**terms, "limit": seen}, ELIGIBLE)
        assert eligible["has_more"] == {"measures": len(HIDDEN)}
    finally:
        runtime.close()


def test_a_grant_counts_only_granted_objects(package) -> None:
    root, config = package
    runtime = Runtime.from_config(config, source_path=str(root))
    granted = (
        "dimension.jaffle_customer_name",
        "dimension.jaffle_customer_type",
        "dimension.jaffle_customer_id",
    )
    context = RequestContext(
        actor="subject",
        roles=("analyst",),
        audience="finance",
        metric_allowlist=("metric.sales.customer_count",),
        dimension_allowlist=granted,
    )
    try:
        response = SemanticLayerMCPAdapter(runtime).call_tool(
            "discover",
            {"terms": "customer", "kinds": ["dimension"], "limit": 1},
            request_context=context,
        )
        assert response["ok"], response
        # The package has many more customer dimensions; only the granted ones count.
        assert response["has_more"] == {"dimensions": len(granted) - 1}
    finally:
        runtime.close()
