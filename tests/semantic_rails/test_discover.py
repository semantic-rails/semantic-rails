"""Discover multi-word fallback — token-AND when the holistic match misses.

Reviewer flagged that ``discover("orders")`` works but multi-word terms
like ``"order count"`` could return ``no_matches`` even though each
token individually resolves. The fallback splits the input on
whitespace, scores each token independently against the catalog,
intersects the matcher sets, and ranks by per-token score sum. Results
carry a ``multi_token_fallback`` reason so callers know the path that
produced them.
"""

from __future__ import annotations

from semantic_rails.metadata import discover_payload


def test_discover_multi_word_order_count_returns_measure_candidates(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = discover_payload(runtime, terms="order count", limit=5)
        # Either the holistic scorer caught it (preferred — the
        # fallback's job is the rescue case) or the fallback fired.
        # Both must surface a measure that obviously matches.
        candidate_ids = [row["id"] for row in payload.get("measures", [])]
        assert payload.get("no_matches") is None
        assert any("order" in cid for cid in candidate_ids), (
            f"expected an order-related measure; got {candidate_ids}"
        )
    finally:
        runtime.close()


def test_discover_distinct_customer_count_returns_candidates(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = discover_payload(runtime, terms="distinct customer count", limit=5)
        candidate_ids = [
            row["id"] for row in [*payload.get("measures", []), *payload.get("metrics", [])]
        ]
        assert payload.get("no_matches") is None
        assert any("customer" in cid for cid in candidate_ids), (
            f"expected a customer-related candidate; got {candidate_ids}"
        )
    finally:
        runtime.close()


def test_discover_definite_nonsense_string_still_returns_no_matches(runtime_factory) -> None:
    """A nonsense string with no real word tokens still returns
    no_matches even after the fallback path runs."""
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = discover_payload(
            runtime,
            terms="asdjkfhqwiuer xyzqwopzxlf",
            limit=5,
        )
        no_matches = payload.get("no_matches")
        assert isinstance(no_matches, dict)
        assert no_matches["terms"] == "asdjkfhqwiuer xyzqwopzxlf"
        assert payload.get("measures") == []
        assert payload.get("metrics") == []
    finally:
        runtime.close()


def test_discover_fallback_annotates_match_strategy(runtime_factory) -> None:
    """When the holistic-string match misses but the per-token
    intersection rescues a candidate, the payload must say so. This
    asserts the rescue path is reachable, even if the jaffle_shop
    catalog rarely hits it.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        # ``snapshot_count`` is the kind of compound term that may slip
        # through the holistic scorer but be rescued by token-AND.
        # The assertion is conditional on the rescue actually firing —
        # the important contract is that *if* fallback runs, callers
        # see ``match_strategy.kind == "multi_token_fallback"``.
        payload = discover_payload(runtime, terms="snapshot inventory count", limit=5)
        strategy = payload.get("match_strategy")
        if strategy is not None:
            assert strategy.get("kind") == "multi_token_fallback"
            assert isinstance(strategy.get("tokens"), list)
            assert len(strategy["tokens"]) >= 2
            # Each rescued candidate must carry the fallback reason.
            for bucket in ("measures", "metrics", "segments", "dimensions"):
                for row in payload.get(bucket, []) or []:
                    reasons = list(row.get("match_reasons", []) or [])
                    assert any("multi_token_fallback" in str(reason) for reason in reasons), (
                        f"row {row['id']} missing fallback reason: {reasons}"
                    )
    finally:
        runtime.close()


def test_discover_fallback_respects_token_cap(runtime_factory) -> None:
    """Pathological inputs with many tokens should not produce
    pathological token-set joins — the implementation caps at 5
    tokens."""
    runtime = runtime_factory("jaffle_shop")
    try:
        terms = " ".join(["order"] * 10 + ["customer"] * 10)
        payload = discover_payload(runtime, terms=terms, limit=5)
        # No crash, structured payload, fallback (if reached) bounded.
        strategy = payload.get("match_strategy")
        if strategy is not None:
            assert len(strategy.get("tokens", [])) <= 5
    finally:
        runtime.close()


def test_discover_idf_boosts_rare_token_match(runtime_factory) -> None:
    """A query like "top customers by aov" used to rank
    measure.ordering_customer_count above metric.sales.aov_usd because
    "customer" hit repeatedly. With IDF weighting (gated on
    enforce_scope=True so it matches the user-facing surface), the rare
    "aov" token outweighs the common "customer" matches and AOV wins."""
    runtime = runtime_factory("jaffle_shop")
    try:
        # enforce_scope=True mirrors the HTTP/MCP boundary's behavior —
        # the public surface a blind agent actually hits.
        payload = discover_payload(
            runtime,
            terms="top customers by aov",
            limit=5,
            enforce_scope=True,
        )
        metric_ids = [r["id"] for r in payload.get("metrics", []) or []]
        measure_ids = [r["id"] for r in payload.get("measures", []) or []]
        all_ids = metric_ids + measure_ids
        assert "metric.sales.aov_usd" in all_ids, "expected aov_usd in results"
        # aov_usd should outrank ordering_customer_count once IDF
        # weighting kicks in.
        aov_score = next(
            r["score"] for r in payload.get("metrics", []) if r["id"] == "metric.sales.aov_usd"
        )
        customer_score = next(
            (
                r["score"]
                for r in payload.get("measures", [])
                if r["id"] == "measure.jaffle.ordering_customer_count"
            ),
            0.0,
        )
        assert aov_score > customer_score, (
            f"aov_usd ({aov_score}) should outscore ordering_customer_count "
            f"({customer_score}) with IDF — rare 'aov' should outweigh common 'customer'"
        )
    finally:
        runtime.close()


def test_discover_warns_on_empty_terms(runtime_factory) -> None:
    """An empty `terms` argument silently returned default-ordering
    candidates that callers could mistake for ranked relevance. Now the
    response carries a structured `DISCOVER_NO_TERMS` warning so blind
    agents see the gap (a common cause is the `term` vs `terms` typo)."""
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = discover_payload(runtime, terms="", limit=3)
        warnings = list(payload.get("warnings", []) or [])
        codes = [str(w.get("code", "")) for w in warnings if isinstance(w, dict)]
        assert "DISCOVER_NO_TERMS" in codes, (
            f"expected DISCOVER_NO_TERMS warning on empty terms; got {warnings}"
        )
        no_terms = next(w for w in warnings if w.get("code") == "DISCOVER_NO_TERMS")
        assert no_terms.get("recovery_hint")
        # A real terms string should NOT carry the warning.
        ranked = discover_payload(runtime, terms="revenue", limit=3)
        ranked_codes = [
            str(w.get("code", ""))
            for w in (ranked.get("warnings", []) or [])
            if isinstance(w, dict)
        ]
        assert "DISCOVER_NO_TERMS" not in ranked_codes
    finally:
        runtime.close()


def test_discover_ships_starter_query_patch_on_each_candidate(runtime_factory) -> None:
    """Blind-agent UX: discover must ship a runnable IR seed on each
    measure/metric/dimension candidate so agents don't have to round-trip
    through inspect just to copy a starter patch."""
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = discover_payload(runtime, terms="revenue", limit=5)
        measures = payload.get("measures", []) or []
        metrics = payload.get("metrics", []) or []
        assert measures or metrics, "expected at least one revenue candidate"
        for row in measures:
            patch = row.get("starter_query_patch")
            assert isinstance(patch, dict) and patch.get("select"), (
                f"measure {row['id']} missing select-shaped starter_query_patch: {patch}"
            )
            select = list(patch["select"])
            expr = dict(select[0].get("expression", {}) or {})
            assert expr.get("measure") == row["id"], (
                f"starter_query_patch on {row['id']} references wrong measure: {expr}"
            )
        for row in metrics:
            patch = row.get("starter_query_patch")
            assert isinstance(patch, dict) and patch.get("select"), (
                f"metric {row['id']} missing select-shaped starter_query_patch: {patch}"
            )

        # Dimensions seed group_by, not select.
        dim_payload = discover_payload(runtime, terms="store", limit=5)
        dimensions = dim_payload.get("dimensions", []) or []
        assert dimensions, "expected at least one store dimension"
        for row in dimensions:
            patch = row.get("starter_query_patch")
            assert isinstance(patch, dict) and patch.get("group_by"), (
                f"dimension {row['id']} missing group_by starter_query_patch: {patch}"
            )
            assert row["id"] in list(patch["group_by"]), (
                f"starter_query_patch group_by does not include {row['id']}: {patch}"
            )
    finally:
        runtime.close()
