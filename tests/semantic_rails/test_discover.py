"""``discover`` ranks multi-word terms, warns on empty ones, and seeds a starter call per card."""

from __future__ import annotations

import pytest

from semantic_rails.metadata import discover_payload
from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    path = copy_package_config(tmp_path_factory.mktemp("package"), "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_snapshot(load_package_snapshot(path))
    yield opened(runtime)
    runtime.close()


@pytest.mark.parametrize(
    ("terms", "buckets", "needle"),
    [
        ("order count", ["measures"], "order"),
        ("distinct customer count", ["measures", "metrics"], "customer"),
        # Repeated tokens stay a bounded search.
        (" ".join(["order"] * 10 + ["customer"] * 10), ["measures"], "order"),
    ],
    ids=["order-count", "distinct-customer-count", "repeated-tokens"],
)
def test_discover_ranks_multi_word_terms(runtime, terms, buckets, needle):
    payload = discover_payload(runtime, terms=terms, limit=5)
    ids = [row["id"] for bucket in buckets for row in payload[bucket]]
    assert payload.get("no_matches") is None
    assert any(needle in object_id for object_id in ids), ids


def test_discover_idf_boosts_rare_token_match(runtime) -> None:
    """The rare "aov" outweighs the common "customer" on the public (scoped) surface."""
    payload = discover_payload(runtime, terms="top customers by aov", limit=5, enforce_scope=True)
    aov = next(r["score"] for r in payload["metrics"] if r["id"] == "metric.sales.aov_usd")
    customers = next(
        (
            r["score"]
            for r in payload["measures"]
            if r["id"] == "measure.jaffle.ordering_customer_count"
        ),
        0.0,
    )
    assert aov > customers


def test_discover_warns_on_empty_terms(runtime) -> None:
    """Empty terms list in default order, which a caller could mistake for a ranking."""
    warnings = discover_payload(runtime, terms="", limit=3)["warnings"]
    (no_terms,) = [w for w in warnings if w.get("code") == "DISCOVER_NO_TERMS"]
    assert no_terms.get("recovery_hint")
    ranked = discover_payload(runtime, terms="revenue", limit=3)
    assert "DISCOVER_NO_TERMS" not in [w.get("code") for w in ranked.get("warnings") or []]


def test_discover_ships_starter_query_patch_on_each_candidate(runtime) -> None:
    """Each measure and metric card seeds a select; each dimension card seeds a group_by."""
    payload = discover_payload(runtime, terms="revenue", limit=5)
    assert payload["measures"] or payload["metrics"]
    for row in payload["measures"]:
        [item] = row["starter_query_patch"]["select"]
        expr = item if isinstance(item, str) else item.get("expression")
        assert (expr if isinstance(expr, str) else dict(expr or {}).get("measure")) == row["id"]
    for row in payload["metrics"]:
        assert row["starter_query_patch"]["select"]
    dimensions = discover_payload(runtime, terms="store", limit=5)["dimensions"]
    assert dimensions
    for row in dimensions:
        assert row["id"] in list(row["starter_query_patch"]["group_by"])
