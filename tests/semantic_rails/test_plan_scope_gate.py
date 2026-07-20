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

import pytest

from semantic_rails.metadata import discover_payload
from tests.plan_candidate_envelope import plan_candidate_envelope

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
    "top customers by revenue",
    "revenue trend over time",
    "orders per store",
]


@pytest.mark.parametrize("intent", NONSENSE_INTENTS)
def test_plan_blocks_nonsense_intents_with_low_relevance(runtime_factory, intent):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(runtime, intent=intent)
    finally:
        runtime.close()
    assert payload["candidates"] == [], (
        f"nonsense intent {intent!r} should yield zero candidates, got {len(payload['candidates'])}"
    )
    assert "low_relevance" in payload, (
        f"nonsense intent {intent!r} should surface a low_relevance block"
    )
    block = payload["low_relevance"]
    assert block["code"] == "LOW_RELEVANCE"
    assert block["intent"] == intent
    assert block["overlap_tokens"] == []
    assert block["recovery_hint"]
    assert block["catalog_token_sample"], "low_relevance should sample catalog tokens"


@pytest.mark.parametrize("intent,expected_category", CLASSIFIER_ROUTED_INTENTS)
def test_plan_routes_non_data_query_intents_out_of_scope(
    runtime_factory, intent, expected_category
):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(runtime, intent=intent)
    finally:
        runtime.close()
    assert payload["candidates"] == [], (
        f"classifier-routed intent {intent!r} should yield zero candidates"
    )
    assert "out_of_scope" in payload, (
        f"classifier-routed intent {intent!r} should surface an out_of_scope block"
    )
    block = payload["out_of_scope"]
    assert block["code"] == "OUT_OF_SCOPE"
    assert block["category"] == expected_category
    assert block["recommended_handoff"]
    assert block["recovery_hint"]


@pytest.mark.parametrize("intent", REASONABLE_INTENTS)
def test_plan_lets_reasonable_intents_through(runtime_factory, intent):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(runtime, intent=intent)
    finally:
        runtime.close()
    assert payload["candidates"], f"reasonable intent {intent!r} should still produce candidates"
    assert "out_of_scope" not in payload
    assert "low_relevance" not in payload


@pytest.mark.parametrize("intent", NONSENSE_INTENTS)
def test_discover_blocks_nonsense_terms_when_enforce_scope_true(runtime_factory, intent):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = discover_payload(runtime, terms=intent, enforce_scope=True)
    finally:
        runtime.close()
    for bucket in ("measures", "metrics", "segments", "dimensions", "dimension_values", "entities"):
        assert payload[bucket] == [], (
            f"nonsense terms {intent!r} should empty bucket {bucket}, got {payload[bucket]}"
        )
    assert "low_relevance" in payload


@pytest.mark.parametrize("intent,expected_category", CLASSIFIER_ROUTED_INTENTS)
def test_discover_routes_non_data_query_terms_out_of_scope(
    runtime_factory, intent, expected_category
):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = discover_payload(runtime, terms=intent, enforce_scope=True)
    finally:
        runtime.close()
    assert "out_of_scope" in payload
    assert payload["out_of_scope"]["category"] == expected_category


def test_discover_internal_callers_unaffected(runtime_factory):
    """build-options/plan's own internal discover calls do not set
    ``enforce_scope=True`` — they must keep working on partial / fuzzy
    terms that would otherwise classify-trip."""
    runtime = runtime_factory("jaffle_shop")
    try:
        # No enforce_scope -> backwards-compatible behavior.
        payload = discover_payload(runtime, terms="why average", limit=3)
    finally:
        runtime.close()
    assert "out_of_scope" not in payload
    # The discover noise floor (cf6edbc) still applies — but we expect at
    # least one real-match candidate from "average".
    assert payload["metrics"] or payload["measures"]


def test_plan_blind_benchmark_corpus_still_passes(runtime_factory):
    """The 14-case benchmark intents in ``scripts/blind_agent_corpus.json``
    must all classify as data_query AND survive the relevance floor."""
    import json
    from pathlib import Path

    corpus_path = Path(__file__).resolve().parents[2] / "scripts" / "blind_agent_corpus.json"
    raw = json.loads(corpus_path.read_text())
    cases = raw.get("benchmark_cases", raw) if isinstance(raw, dict) else raw
    runtime = runtime_factory("jaffle_shop")
    try:
        for case in cases:
            intent = case.get("intent") or case.get("question") or ""
            if not intent:
                continue
            payload = plan_candidate_envelope(runtime, intent=intent)
            assert "out_of_scope" not in payload, (
                f"benchmark intent {intent!r} unexpectedly routed out of scope"
            )
            assert "low_relevance" not in payload, (
                f"benchmark intent {intent!r} unexpectedly tripped relevance floor"
            )
    finally:
        runtime.close()


# Intents that classify + clear the relevance floor but cannot resolve to a
# single executable candidate because the requested entity path / analytic
# pattern isn't modeled in the package. The v2 adversarial audit called out
# YoY-active-customers as the canonical example: an agent receives
# ``candidates=[]`` with no top-level gate signal and starts guessing.
NO_VIABLE_INTENTS = [
    "inventory by membership status",
    "high value customer count by segment membership",
]


@pytest.mark.parametrize("intent", NO_VIABLE_INTENTS)
def test_plan_emits_no_viable_signal_when_all_candidates_blocked(runtime_factory, intent):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(runtime, intent=intent)
    finally:
        runtime.close()
    assert payload["candidates"] == [], (
        f"intent {intent!r} should produce zero candidates (precondition for the gate signal)"
    )
    assert payload["blocked"], f"intent {intent!r} should have at least one blocked entry"
    assert "no_viable_candidates" in payload, (
        f"intent {intent!r} should surface a no_viable_candidates block "
        f"at the envelope level — got keys: {sorted(payload.keys())}"
    )
    block = payload["no_viable_candidates"]
    assert block["code"] == "NO_VIABLE_CANDIDATES"
    assert block["intent"] == intent
    assert block["blocked_codes"], "blocked_codes must list the reason codes"
    assert block["recovery_hint"], "recovery_hint must steer the agent"
    # The signal must NOT clobber other gate blocks when they're present.
    assert "low_relevance" not in payload
    assert "out_of_scope" not in payload


# ----------------------------------------------------------------------
# Discover empty-set signal — nonsense terms must not surface
# bookkeeping-only matches. Previously, a random string returned three
# metrics scored ~29 on bookkeeping bonuses alone (``preferred kind``,
# ``review priority high``). Agents had no clear signal that nothing
# matched.
# ----------------------------------------------------------------------


def test_discover_nonsense_term_returns_empty_with_no_matches_block(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = discover_payload(runtime, terms="asdjkfhqwiuer", limit=5)

        assert payload.get("metrics") == []
        assert payload.get("measures") == []
        assert payload.get("dimensions") == []
        assert payload.get("dimension_values") == []
        assert payload.get("entities") == []
        assert payload.get("segments") == []

        no_matches = payload.get("no_matches")
        assert isinstance(no_matches, dict), "expected no_matches block on empty result"
        assert no_matches.get("terms") == "asdjkfhqwiuer"
        assert "matched" in no_matches.get("reason", "")
        assert no_matches.get("recovery_hint")
    finally:
        runtime.close()


def test_discover_real_term_still_returns_candidates(runtime_factory) -> None:
    """Filter must not break the happy path — real terms still find rows."""
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = discover_payload(runtime, terms="average order value", limit=5)
        assert payload.get("metrics"), "real terms should still return metric matches"
        assert payload.get("no_matches") is None
    finally:
        runtime.close()
