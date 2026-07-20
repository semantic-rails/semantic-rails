"""Plan hardening for qualified-metric rollups + inline YoY.

Phase 5 contracts (see plan ``snoopy-foraging-crescent.md``):

- When ``interpreted_intent.pattern == "qualified_metric_rollup"``, the
  produced candidate IR MUST contain a non-empty ``metric_filters`` and
  a ``limit``. If those can't be synthesized, the candidate is dropped
  to ``blocked`` with code ``QUALIFICATION_NOT_REALIZABLE``.

- ``predicate_metrics`` resolution prefers measures/metrics whose
  label/search_terms match the qualification phrase tokens
  (e.g. "distinct customers" → ``ordering_customer_count`` or
  ``customer_count``), and falls back to "no match" rather than
  picking an unrelated measure (e.g. ``aov_usd``).

- The produced IR validates against its declared stable/preview schema.

- "Revenue with YoY"-style intents emit the inline ``prior_period``
  shorthand from Phase 4.
"""

from __future__ import annotations

import json
import pathlib

import pytest

try:
    import jsonschema
except ImportError:  # pragma: no cover - matches schema-test guard
    jsonschema = None

from tests.plan_candidate_envelope import plan_candidate_envelope

SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[2] / "schemas" / "query_ir.v1.json"
PREVIEW_V2_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "schemas" / "query_ir.preview.v2.json"
)


def _load_schema(version: int) -> dict:
    path = PREVIEW_V2_SCHEMA_PATH if version == 2 else SCHEMA_PATH
    return json.loads(path.read_text())


def _validate_against_schema(query: dict) -> None:
    if jsonschema is None:
        pytest.skip("jsonschema is not installed in this environment")
    jsonschema.Draft202012Validator(_load_schema(int(query.get("version", 1)))).validate(query)


def _resolved_ids(candidate: dict) -> list[str]:
    return [row.get("id", "") for row in (candidate.get("resolved", []) or [])]


def test_predicate_metric_resolves_to_customer_count_not_aov(runtime_factory) -> None:
    """The reviewer's flagship failure: 'with at least N distinct
    customers' silently resolved ``predicate_metrics`` to
    ``metric.sales.aov_usd``. After phase 5 it must resolve to one of
    the customer-count measures/metrics — never AOV.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(
            runtime,
            intent="top stores by order count with at least 4 distinct customers",
            limit=5,
        )
        assert payload["interpreted_intent"]["pattern"] == "qualified_metric_rollup"
        predicate_metrics = list(payload["interpreted_intent"].get("predicate_metrics", []) or [])
        assert predicate_metrics, (
            "predicate_metrics must resolve — without a predicate the qualification is unrealizable"
        )
        # Never AOV — that was the bug.
        assert "metric.sales.aov_usd" not in predicate_metrics
        # Must be one of the customer-count primitives.
        allowed = {
            "measure.jaffle.ordering_customer_count",
            "measure.jaffle.customer_count",
            "metric.sales.customer_count",
        }
        assert set(predicate_metrics) & allowed, (
            f"expected a customer-count predicate; got {predicate_metrics}"
        )
    finally:
        runtime.close()


def test_qualified_rollup_synthesizes_metric_filters_order_by_limit(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(
            runtime,
            intent="top 3 stores by order count with at least 4 distinct customers",
            limit=5,
        )
        assert payload["candidates"], "expected a runnable candidate"
        candidate = payload["candidates"][0]
        query = candidate["candidate_ir"]
        assert candidate["validation"]["ok"] is True
        # metric_filters must be present and non-empty (phase 5 contract)
        metric_filters = query.get("metric_filters", []) or []
        assert metric_filters, "qualified_metric_rollup must emit metric_filters"
        first_filter = metric_filters[0]
        expression = first_filter["expression"]
        assert expression["kind"] == "metric_predicate"
        assert expression["entity"] == "entity.jaffle_store"
        assert expression["op"] == ">="
        assert expression["value"] == 4
        assert ("measure" in expression["input"]) or ("metric" in expression["input"])
        # order_by ranks the target metric descending for top-N intents.
        order_by = query.get("order_by", []) or []
        assert order_by, "top-N qualified rollups must emit order_by"
        assert order_by[0]["direction"].upper() == "DESC"
        # limit must be present and equal to the parsed top-N (3).
        assert query.get("limit") == 3
        # IR validates against the published schema.
        _validate_against_schema(query)
    finally:
        runtime.close()


def test_qualified_rollup_default_limit_when_no_top_n(runtime_factory) -> None:
    """When the intent omits a "top N" count but still triggers the
    qualified rollup, the IR must carry a default limit (phase 5
    contract: every qualified_metric_rollup IR has a limit).
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(
            runtime,
            intent="monthly order volume for customers that made more than 10 purchases in that month",
            limit=2,
        )
        assert payload["candidates"]
        query = payload["candidates"][0]["candidate_ir"]
        assert query.get("limit") is not None
        assert query.get("metric_filters")
        _validate_against_schema(query)
    finally:
        runtime.close()


def test_qualification_not_realizable_drops_to_blocked(runtime_factory) -> None:
    """When the pattern is matched (threshold + entity + target) but no
    predicate metric can be resolved from the qualification phrase, the
    candidate must drop to blocked with QUALIFICATION_NOT_REALIZABLE.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        # An intent whose qualification phrase has NO catalog-tokens
        # that resolve to any measure or metric. The threshold is
        # present, the entity is present, the target is present —
        # only the predicate target can't be resolved.
        payload = plan_candidate_envelope(
            runtime,
            intent="top stores by order count with at least 4 puffins of fluff",
            limit=3,
            verbosity="full",
        )
        # The qualified path should mark this as blocked. Note that
        # discovery may still surface unrelated fallback candidates —
        # what matters is that the qualified pattern itself did NOT
        # silently produce a degenerate IR.
        blocked_codes = {
            row.get("why_blocked", {}).get("code") for row in (payload.get("blocked", []) or [])
        }
        assert "QUALIFICATION_NOT_REALIZABLE" in blocked_codes, (
            f"expected QUALIFICATION_NOT_REALIZABLE in blocked codes; got {blocked_codes}"
        )
        # interpreted_intent still names the matched pattern with
        # predicate_metrics = [] so the caller knows where the
        # resolution failed.
        ii = payload.get("interpreted_intent")
        if isinstance(ii, dict):
            assert ii.get("pattern") == "qualified_metric_rollup"
            assert ii.get("predicate_metrics") == []
        # The blocked envelope carries recovery hints naming the
        # qualification phrase that couldn't be resolved.
        blocked_rows = [
            row
            for row in (payload.get("blocked", []) or [])
            if row.get("why_blocked", {}).get("code") == "QUALIFICATION_NOT_REALIZABLE"
        ]
        assert blocked_rows
        row = blocked_rows[0]
        assert row.get("recovery_hints"), "QUALIFICATION_NOT_REALIZABLE must carry recovery_hints"
        details = row["why_blocked"].get("details", {}) or {}
        assert "qualification_phrase" in details
    finally:
        runtime.close()


def test_inline_yoy_emits_prior_period_shorthand(runtime_factory) -> None:
    """'revenue with YoY' (and 'year over year') emit a 2-column IR
    using the inline prior_period shorthand from Phase 4.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        for intent in (
            "revenue with YoY",
            "revenue year over year",
            "revenue vs last year",
        ):
            payload = plan_candidate_envelope(runtime, intent=intent, limit=3)
            candidates = list(payload.get("candidates", []) or [])
            assert candidates, f"no candidates for {intent!r}"
            candidate = candidates[0]
            query = candidate["candidate_ir"]
            assert candidate["validation"]["ok"] is True, f"{intent!r} candidate must validate"
            selects = query.get("select", []) or []
            assert len(selects) == 2, (
                f"{intent!r} must produce a 2-column select; got {len(selects)}"
            )
            kinds = []
            for select in selects:
                expr = select.get("expression", {}) or {}
                kinds.append(expr.get("kind", ""))
            assert "prior_period" in kinds, (
                f"{intent!r} must include a prior_period expression; got {kinds}"
            )
            # The prior_period select uses the shorthand form.
            prior = next(
                select
                for select in selects
                if (select.get("expression", {}) or {}).get("kind") == "prior_period"
            )["expression"]
            assert "measure" in prior
            assert "offset" in prior
            assert "grain" in prior
            assert prior["grain"] == "year"
            assert prior["offset"] == -1
            _validate_against_schema(query)
    finally:
        runtime.close()
