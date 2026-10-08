"""Every exclusion needs its own evidence, even beside another negative filter."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import duckdb
import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.planner.faithfulness import intent_faithfulness_why
from semantic_rails.planner.filter_checks import _negative_filter_evidence
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ValueDomainConfig, ValueDomainValue
from tests.semantic_rails.conftest import opened
from tests.semantic_rails.result_helpers import assert_plan_held
from tests.semantic_rails.test_plan_accuracy_guard import _draft_plan

CHANNEL = "dimension.shop_customer_channel"
NOW = {"now": "2024-07-05T06:00:00Z"}
WHERE = [{"field": CHANNEL, "op": "!=", "value": "store"}]
GAP = "PLAN_INTENT_COVERAGE_GAP"


@pytest.fixture()
def shop(tmp_path):
    source = Path(__file__).resolve().parents[1] / "integration" / "correctness" / "shop"
    package = tmp_path / "shop"
    shutil.copytree(source, package)
    runtime = opened(Runtime.from_path(str(package)))
    runtime._config = replace(
        runtime._config,
        value_domains=[
            ValueDomainConfig(
                id="value_domain.channels",
                dimensions=[CHANNEL],
                values=[
                    ValueDomainValue(value="store", label="Store", aliases=["store channel"]),
                    ValueDomainValue(value="web", label="Web"),
                ],
            )
        ],
    )
    try:
        yield runtime
    finally:
        runtime.close()


def _draft():
    return {
        "version": 1,
        "select": [{"as": "signups", "expression": {"measure": "measure.shop.signup_count"}}],
        "where": WHERE,
        "policy_context": NOW,
    }


def _reference(shop, sql):
    with duckdb.connect(shop.db_path, read_only=True) as connection:
        return connection.execute(sql).fetchone()[0]


def _assert_unrealized(payload):
    assert_plan_held(payload, GAP)
    gaps = payload["why"]["details"]["gaps"]
    assert "negation_unrealized" in {gap["kind"] for gap in gaps}
    gap = next(gap for gap in gaps if gap["kind"] == "negation_unrealized")
    assert gap["actual"]["negative_predicate_present"] is False


@pytest.fixture()
def listed_channels(shop):
    shop._config = replace(
        shop._config,
        value_domains=[
            ValueDomainConfig(
                id="value_domain.channels",
                dimensions=[CHANNEL],
                values=[
                    ValueDomainValue(value="web", label="Web"),
                    ValueDomainValue(value="store", label="Top"),
                ],
            )
        ],
        measures=[
            replace(
                measure,
                description="Count distinct customers signing up through web and Top channels.",
            )
            if measure.id == "measure.shop.signup_count"
            else measure
            for measure in shop._config.measures
        ],
    )
    return shop


@pytest.mark.parametrize(
    ("op", "value", "status", "draft_count"),
    [
        ("!=", "web", "low_confidence", 3),
        ("NOT IN", ["web", "store"], "ok", 0),
    ],
)
def test_every_named_exclusion_value_needs_a_negative_predicate(
    listed_channels, op, value, status, draft_count
):
    reference = _reference(
        listed_channels,
        "SELECT COUNT(DISTINCT customer_id) FROM signups WHERE channel NOT IN ('web', 'store')",
    )
    assert reference == 0
    payload = plan_payload(
        listed_channels,
        intent='signups excluding web and "Top"',
        partial_query={
            "where": [{"field": CHANNEL, "op": op, "value": value}],
            "policy_context": NOW,
        },
    )
    count = listed_channels.query(payload["best"]["query_ir"])["rows"][0]["signup_count"]
    assert count == draft_count
    if status == "ok":
        assert payload["status"] == "ok", payload
        assert payload["next"]["ready_for"] == ["execute"]
        assert count == reference
    else:
        _assert_unrealized(payload)
        assert count != reference


def test_an_unknown_exclusion_list_item_cannot_disappear(listed_channels):
    reference = _reference(
        listed_channels,
        "SELECT COUNT(DISTINCT customer_id) FROM signups WHERE channel NOT IN ('web', 'partner')",
    )
    assert reference == 3
    payload = plan_payload(
        listed_channels,
        intent="signups excluding web and partner",
        partial_query={
            "where": [{"field": CHANNEL, "op": "!=", "value": "web"}],
            "policy_context": NOW,
        },
    )
    assert listed_channels.query(payload["best"]["query_ir"])["rows"] == [
        {"signup_count": reference}
    ]
    assert_plan_held(payload, "PLAN_UNMATCHED_TERMS")
    assert payload["why"]["details"]["terms"] == ["partner"]


@pytest.mark.parametrize(
    ("question", "start", "end", "expected"),
    [
        ("signups not in June 2024", "2024-06-01", "2024-07-01", 2),
        ("signups not in 2024", "2024-01-01", "2025-01-01", 1),
    ],
)
def test_temporal_exclusion_is_not_realized_by_an_unrelated_negative_filter(
    shop, question, start, end, expected
):
    reference = _reference(
        shop,
        "SELECT COUNT(DISTINCT customer_id) FROM signups WHERE channel != 'store' "
        f"AND NOT (signed_up_at >= TIMESTAMP '{start}' AND signed_up_at < TIMESTAMP '{end}')",
    )
    assert reference == expected
    payload = plan_payload(
        shop, intent=question, partial_query={"where": WHERE, "policy_context": NOW}
    )
    _assert_unrealized(payload)
    # The diagnostic IR still carries the positive window, whose count is wrong for the question.
    query = payload["best"]["query_ir"]
    assert query["time"]["start"] == start
    assert query["time"]["end"] == end
    assert shop.query(query)["rows"][0]["signup_count"] != reference


@pytest.mark.parametrize(
    ("question", "op", "value", "expected"),
    [
        ("signups not from the store channel", "!=", "store", 3),
        ("signups not from the store channel", "NOT IN", ["store"], 3),
        ("signups excluding web", "!=", "web", 3),
        ("signups excluding web", "!=", "store", 0),
        ("signups excluding partner", "!=", "store", 3),
        ("signups excluding partner", "!=", "partner", 6),
    ],
)
def test_only_a_matching_negative_predicate_realizes_an_exclusion(
    shop, monkeypatch, question, op, value, expected
):
    # Supply the draft directly to exercise the shared gate independently of filter generation.
    draft = {**_draft(), "where": [{"field": CHANNEL, "op": op, "value": value}]}
    _draft_plan(monkeypatch, draft)
    excluded = "store" if "store channel" in question else question.split()[-1]
    matching = value == excluded or value == [excluded]
    reference = _reference(
        shop,
        f"SELECT COUNT(DISTINCT customer_id) FROM signups WHERE channel != '{excluded}'"
        + (" AND channel != 'store'" if not matching else ""),
    )
    assert reference == expected
    payload = plan_payload(shop, intent=question, partial_query={"policy_context": NOW})
    if matching and excluded in {"store", "web"}:
        assert (
            intent_faithfulness_why(
                shop, question=question, intent_ir=parse_intent(shop, question), query=draft
            )
            is None
        )
        if "from" in question:
            # This phrasing already has an independent grouping hold; never widen its readiness.
            assert_plan_held(payload, "PLAN_UNMATCHED_TERMS")
        else:
            assert payload["status"] == "ok", payload.get("why")
            assert payload["next"]["ready_for"] == ["execute"]
        assert shop.query(payload["best"]["query_ir"])["rows"] == [{"signups": reference}]
    else:
        _assert_unrealized(payload)


@pytest.mark.parametrize(
    ("phrase", "start", "end", "expected"),
    [
        ("June 2024", "2024-06-01", "2024-07-01", 2),
        ("in 2024", "2024-01-01", "2025-01-01", 1),
        ("last month", "2024-06-01", "2024-07-01", 2),
        ("today", "2024-07-05", "2024-07-06", 3),
    ],
)
def test_temporal_exclusion_holds_even_when_a_catalog_value_matches(
    shop, monkeypatch, phrase, start, end, expected
):
    assert (
        _reference(
            shop,
            "SELECT COUNT(DISTINCT customer_id) FROM signups WHERE channel != 'store' "
            f"AND NOT (signed_up_at >= TIMESTAMP '{start}' AND signed_up_at < TIMESTAMP '{end}')",
        )
        == expected
    )
    domain = shop._config.value_domains[0]
    store = domain.values[0]
    shop._config = replace(
        shop._config,
        value_domains=[
            replace(domain, values=[replace(store, aliases=[phrase]), *domain.values[1:]])
        ],
    )
    assert _negative_filter_evidence(shop, _draft(), phrase)
    _draft_plan(monkeypatch, _draft())
    payload = plan_payload(
        shop, intent=f"signups excluding {phrase}", partial_query={"policy_context": NOW}
    )
    _assert_unrealized(payload)
