"""Plan / discover scope gate — agent-first endpoints must refuse
out-of-domain intents instead of returning confident candidates.

Audit finding I1 / C3: ``/plan`` happily produced a real candidate
IR for ``"compile the linux kernel"`` (Revenue, confidence 0.639) and
``"airspeed velocity of an unladen swallow"`` (Inventory, 0.651). The
marquee claim is "every query is classified, validated, compiled to
SQL, and explained — before a row is fetched"; this test pins that
contract for ``plan`` and ``discover``.

Two-layer gate:
  1. Classifier — if ``classify_question`` routes the intent to a
     non-``data_query`` category, return an ``out_of_scope`` block.
  2. Relevance floor — even within ``data_query``, if no content token
     in the intent overlaps the catalog token index, return a
     ``low_relevance`` block.

Reasonable intents must still return candidates and not trip either gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from semantic_rails.metadata import discover_payload
from semantic_rails.planner import plan_payload
from tests.plan_candidate_envelope import plan_candidate_envelope
from tests.semantic_rails.planner_fixtures import jaffle_runtime
from tests.semantic_rails.result_helpers import held_candidate

NONSENSE_INTENTS = [
    "compile the linux kernel",
    "airspeed velocity of an unladen swallow",
    "blue whale migration patterns",
    "describe the smell of rain on hot asphalt",
]

CLASSIFIER_ROUTED_INTENTS = [
    ("forecast Q4 ARR", "forecasting"),
    ("predict revenue next quarter", "forecasting"),
    ("why is revenue declining", "diagnostic_synthesis"),
    ("send to email the weekly report", "workflow"),
]

REASONABLE_INTENTS = [
    "average order value by month",
    "revenue trend over time",
    "orders per store",
]
BUCKETS = ("measures", "metrics", "segments", "dimensions", "dimension_values", "entities")


runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)


@pytest.mark.parametrize("intent", NONSENSE_INTENTS)
def test_plan_and_discover_block_nonsense_intents_with_low_relevance(runtime, intent):
    payload = plan_candidate_envelope(runtime, intent=intent)
    assert payload["candidates"] == []
    block = payload["low_relevance"]
    assert block["code"] == "LOW_RELEVANCE"
    assert block["intent"] == intent
    assert block["overlap_tokens"] == []
    assert block["recovery_hint"]
    assert block["catalog_token_sample"], "low_relevance should sample catalog tokens"
    found = discover_payload(runtime, terms=intent, enforce_scope=True)
    assert {bucket: found[bucket] for bucket in BUCKETS} == dict.fromkeys(BUCKETS, [])
    assert "low_relevance" in found


@pytest.mark.parametrize("intent,expected_category", CLASSIFIER_ROUTED_INTENTS)
def test_plan_and_discover_route_non_data_query_intents_out_of_scope(
    runtime, intent, expected_category
):
    payload = plan_candidate_envelope(runtime, intent=intent)
    assert payload["candidates"] == []
    block = payload["out_of_scope"]
    assert block["code"] == "OUT_OF_SCOPE"
    assert block["category"] == expected_category
    assert block["recommended_handoff"]
    assert block["recovery_hint"]
    found = discover_payload(runtime, terms=intent, enforce_scope=True)
    assert found["out_of_scope"]["category"] == expected_category


@pytest.mark.parametrize("intent", REASONABLE_INTENTS)
def test_plan_lets_reasonable_intents_through(runtime, intent):
    payload = plan_candidate_envelope(runtime, intent=intent)
    if intent == "orders per store":
        # "store" names the rows: it doesn't pick the same-store conversion rate for "orders".
        held_candidate(payload, "PLAN_INTENT_COVERAGE_GAP")
    else:
        assert payload["candidates"], intent
    assert "out_of_scope" not in payload
    assert "low_relevance" not in payload


@pytest.mark.parametrize(
    ("intent", "window", "limit"),
    [
        ("top customers by revenue", False, 5),
        ("top 10 customers by revenue in Q1 2017", True, 10),
        ("revenue by customer", False, None),
    ],
)
def test_customer_grouping_uses_the_customer_key_and_matches_reference_sql(
    runtime, intent, window, limit
):
    payload = plan_payload(runtime, intent=intent)
    assert "out_of_scope" not in payload
    assert "low_relevance" not in payload
    assert payload["status"] == "ok", payload.get("why")
    assert payload["next"]["ready_for"] == ["execute"]
    query = payload["best"]["query"]
    assert query["group_by"] == ["dimension.jaffle_customer_id"]
    if window:
        # No grain asked: one total per customer over the quarter.
        assert query["time"] == {
            "temporal_role": "temporal_role.jaffle_order_time",
            "start": "2017-01-01",
            "end": "2017-04-01",
        }
    else:
        assert "time" not in query
    assert query.get("limit") == limit
    rows = runtime.query(query)["rows"]
    where = " WHERE ordered_at >= '2017-01-01' AND ordered_at < '2017-04-01'" if window else ""
    rank = f" ORDER BY revenue DESC LIMIT {limit}" if limit else ""
    reference = runtime.adapter.query(
        "SELECT customer_id, SUM(order_total_cents / 100.0) AS revenue"
        f" FROM jaffle_order{where} GROUP BY customer_id{rank}"
    )
    assert {row["dimension.jaffle_customer_id"]: float(row["revenue_usd"]) for row in rows} == (
        pytest.approx({row["customer_id"]: float(row["revenue"]) for row in reference})
    )


def test_discover_without_the_scope_gate_still_finds_real_terms(runtime):
    """plan's own internal discover calls do not set ``enforce_scope=True``: partial or fuzzy
    terms that would trip the classifier keep working, and real terms find rows."""
    payload = discover_payload(runtime, terms="why average", limit=3)
    assert "out_of_scope" not in payload
    assert payload["metrics"] or payload["measures"]
    payload = discover_payload(runtime, terms="average order value", limit=5)
    assert payload.get("metrics"), "real terms should still return metric matches"
    assert payload.get("no_matches") is None


def test_plan_blind_benchmark_corpus_still_passes(runtime):
    """The benchmark intents in ``scripts/blind_agent_corpus.json`` all classify as data_query
    and survive the relevance floor."""
    corpus_path = Path(__file__).resolve().parents[2] / "scripts" / "blind_agent_corpus.json"
    raw = json.loads(corpus_path.read_text())
    cases = raw.get("benchmark_cases", raw) if isinstance(raw, dict) else raw
    for case in cases:
        intent = case.get("intent") or case.get("question") or ""
        if not intent:
            continue
        payload = plan_candidate_envelope(runtime, intent=intent)
        assert "out_of_scope" not in payload, intent
        assert "low_relevance" not in payload, intent


# Intents that classify + clear the relevance floor but cannot resolve to a
# single executable candidate because the requested entity path / analytic
# pattern isn't modeled in the package: an agent receiving ``candidates=[]`` with no
# top-level gate signal starts guessing.
NO_VIABLE_INTENTS = [
    "inventory by membership status",
    "high value customer count by segment membership",
]


@pytest.mark.parametrize("intent", NO_VIABLE_INTENTS)
def test_plan_emits_no_viable_signal_when_all_candidates_blocked(runtime, intent):
    payload = plan_candidate_envelope(runtime, intent=intent)
    assert payload["candidates"] == []
    assert payload["blocked"], f"intent {intent!r} should have at least one blocked entry"
    block = payload["no_viable_candidates"]
    assert block["code"] == "NO_VIABLE_CANDIDATES"
    assert block["intent"] == intent
    assert block["blocked_codes"], "blocked_codes must list the reason codes"
    assert block["recovery_hint"], "recovery_hint must steer the agent"
    # The signal must NOT clobber other gate blocks when they're present.
    assert "low_relevance" not in payload
    assert "out_of_scope" not in payload


def test_discover_nonsense_term_returns_empty_with_no_matches_block(runtime) -> None:
    # Bookkeeping bonuses alone never surface a match: the agent gets a clear empty signal.
    payload = discover_payload(runtime, terms="asdjkfhqwiuer", limit=5)
    assert {bucket: payload.get(bucket) for bucket in BUCKETS} == dict.fromkeys(BUCKETS, [])
    no_matches = payload.get("no_matches")
    assert isinstance(no_matches, dict), "expected no_matches block on empty result"
    assert no_matches.get("terms") == "asdjkfhqwiuer"
    assert no_matches.get("recovery_hint")
