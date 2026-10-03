"""Recovery hints and payload sizes on the MCP surface.

First batch — three reviewer complaints:

1. ``INVALID_EXPRESSION_AST`` for malformed inline ``prior_period``
   (e.g. ``offset: {unit: "month", n: 1}``) used to come back with no
   recovery hints. The enriched envelope now carries the authored
   counterpart in ``closest_matches`` and a structured fix-shape hint.
2. ``PATH_NOT_FOUND`` used to ship empty ``recovery_hints``. The
   enrichment attaches ``reachable_targets`` and an "isolated source"
   hint when no path exists at all.
3. ``catalog`` at compact verbosity used to weigh ~830 KB on jaffle.
   The stripped row schema is now < 250 KB at compact and < 50 KB
   at minimal. ``plan`` honours both modes too.

Second batch (recovery-hint papercuts) — four extensions:

4. Nested parameter schemas (``percentile`` takes ``{p}``) surface
   in ``INVALID_EXPRESSION_KEY`` recovery hints.
5. Cross-position key mismatches (``where`` got ``field``;
   ``order_by`` got ``dimension``) emit a ``use_canonical_key_for_position``
   hint pointing at the right slot.
6. ``OBJECT_NOT_FOUND`` for an id with no semantic neighbours returns
   empty ``closest_matches`` and a ``call_discover_to_locate`` handoff.
7. Execute returning zero rows with a time filter attaches a
   ``data_diagnostics`` field so the agent knows the data is sparse,
   not that the query silently dropped rows.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import catalog_payload
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.plan_candidate_envelope import plan_candidate_envelope


def _counterpart_config(config, missing_kind, with_policy):
    other_kind = "measure" if missing_kind == "metric" else "metric"
    hidden_id = f"{other_kind}.synthetic.private_fee"
    fuzzy_id = f"{missing_kind}.synthetic.private_fees"
    config = replace(
        config,
        measures=[
            replace(config.measures[0], id=hidden_id if other_kind == "measure" else fuzzy_id)
        ],
        metric_recipes=[
            replace(config.metric_recipes[0], id=hidden_id if other_kind == "metric" else fuzzy_id)
        ],
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.synthetic.visibility",
                kind="object_visibility",
                object_ids=[hidden_id, fuzzy_id],
                audiences=["external"],
                action="hidden",
            )
        ]
        if with_policy
        else [],
    )
    return config, hidden_id, fuzzy_id


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_counterpart_visibility_uses_one_package_snapshot(package_config_factory, transport):
    from semantic_rails.errors import SemanticLayerError
    from semantic_rails.http_core import SemanticHTTPService
    from semantic_rails.request_context import RequestContext

    config, package = package_config_factory("jaffle_shop")
    config, hidden_id, _ = _counterpart_config(config, "metric", True)
    config = replace(config, metric_recipes=[])
    reloaded_config = replace(config, measures=[], semantic_policies=[])

    class ReloadingRuntime:
        def __init__(self, runtime):
            self.runtime = runtime
            self.config_reads = 0

        @property
        def _config(self):
            self.config_reads += 1
            return config if self.config_reads == 1 else reloaded_config

        def __getattr__(self, name):
            return getattr(self.runtime, name)

    runtime = Runtime.from_config(config, source_path=str(package))
    proxy = ReloadingRuntime(runtime)
    missing_id = "metric.synthetic.private_fee"
    try:
        if transport == "http":
            response, status = SemanticHTTPService(proxy).exception_payload(
                SemanticLayerError(
                    "OBJECT_NOT_FOUND", "Unknown object", details={"object_id": missing_id}
                ),
                stage="http",
                context=RequestContext(audience="external"),
            )
            assert status == 400
        else:
            response = SemanticLayerMCPAdapter(proxy).call_tool(
                "execute",
                {
                    "query": {"select": [{"expression": {"metric": missing_id}}]},
                    "mode": "sql",
                    "policy_context": {"audience": "external"},
                    "verbosity": "full",
                },
            )
        assert response["ok"] is False
        assert response["errors"][0]["code"] == "OBJECT_NOT_FOUND"
        serialized = json.dumps(response)
        assert hidden_id not in serialized
        # The error still echoes the caller's missing ID; every other value must
        # exclude the private name, including duplicated hints and suggestions.
        assert "private_fee" not in serialized.replace(missing_id, "")
        assert proxy.config_reads == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("missing_kind", ["metric", "measure"])
@pytest.mark.parametrize("mode", ["run", "validate", "sql"])
@pytest.mark.parametrize("with_policy", [False, True], ids=["public", "hidden"])
@pytest.mark.parametrize("context_source", ["outer", "nested", "host"])
def test_missing_counterpart_suggestions_follow_request_visibility(
    package_config_factory, missing_kind, mode, with_policy, context_source
):
    from semantic_rails.request_context import RequestContext

    config, package = package_config_factory("jaffle_shop")
    config, hidden_id, fuzzy_id = _counterpart_config(config, missing_kind, with_policy)
    runtime = Runtime.from_config(config, source_path=str(package))
    try:
        query = {
            "select": [{"expression": {missing_kind: f"{missing_kind}.synthetic.private_fee"}}],
        }
        arguments = {"query": query, "mode": mode, "verbosity": "full"}
        context = {"audience": "external"}
        if context_source == "nested":
            query["policy_context"] = context
        else:
            arguments["policy_context"] = context
        if context_source == "host":
            # Trusted transport context must win over caller-supplied visibility.
            arguments["policy_context"] = {"audience": "internal"}
        response = SemanticLayerMCPAdapter(runtime).call_tool(
            "execute",
            arguments,
            request_context=RequestContext(audience="external")
            if context_source == "host"
            else None,
        )
        assert response["ok"] is False
        issue = response["errors"][0]
        assert issue["code"] == "OBJECT_NOT_FOUND"
        if with_policy:
            serialized = json.dumps(response)
            assert hidden_id not in serialized
            assert fuzzy_id not in serialized
        else:
            assert issue["details"]["closest_matches"][0] == hidden_id
    finally:
        runtime.close()


@pytest.mark.parametrize("missing_kind", ["metric", "measure"])
@pytest.mark.parametrize("audience", ["external", "internal", None])
def test_http_counterpart_suggestions_use_resolved_visibility(
    package_config_factory, missing_kind, audience
):
    from semantic_rails.errors import SemanticLayerError
    from semantic_rails.http_core import SemanticHTTPService
    from semantic_rails.request_context import RequestContext

    config, package = package_config_factory("jaffle_shop")
    config, hidden_id, fuzzy_id = _counterpart_config(config, missing_kind, True)
    runtime = Runtime.from_config(config, source_path=str(package))
    try:
        response, status = SemanticHTTPService(runtime).exception_payload(
            SemanticLayerError(
                "OBJECT_NOT_FOUND",
                "Unknown object",
                details={"object_id": f"{missing_kind}.synthetic.private_fee"},
            ),
            stage="http",
            context=RequestContext(audience=audience) if audience is not None else None,
        )
        assert status == 400
        if audience == "internal":
            assert response["error"]["details"]["closest_matches"][0] == hidden_id
        else:
            assert hidden_id not in json.dumps(response)
            if audience == "external":
                assert fuzzy_id not in json.dumps(response)
            else:
                assert response["error"]["details"]["closest_matches"] == [fuzzy_id]
    finally:
        runtime.close()


@pytest.mark.parametrize("missing_kind", ["metric", "measure"])
@pytest.mark.parametrize("mode", ["run", "validate", "sql"])
def test_unavailable_visibility_cannot_add_a_counterpart(
    package_config_factory, monkeypatch, missing_kind, mode
):
    from semantic_rails import policies

    config, package = package_config_factory("jaffle_shop")
    config, hidden_id, _ = _counterpart_config(config, missing_kind, True)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Visibility unavailable")

    monkeypatch.setattr(policies, "hidden_object_ids", unavailable)
    runtime = Runtime.from_config(config, source_path=str(package))
    try:
        response = SemanticLayerMCPAdapter(runtime).call_tool(
            "execute",
            {
                "query": {
                    "select": [
                        {"expression": {missing_kind: f"{missing_kind}.synthetic.private_fee"}}
                    ]
                },
                "mode": mode,
                "policy_context": {"audience": "external"},
            },
        )
        assert response["errors"][0]["code"] == "OBJECT_NOT_FOUND"
        assert hidden_id not in json.dumps(response)
    finally:
        runtime.close()


@pytest.mark.parametrize("op", ["is_null", "is_not_null", "unknown"])
def test_where_operator_refusal_names_query_operators_and_null_form(runtime_factory, op):
    runtime = runtime_factory("jaffle_shop")
    try:
        adapter = SemanticLayerMCPAdapter(runtime)
        query = {
            "select": [{"expression": {"measure": "measure.jaffle.order_count"}}],
            "where": [{"field": "dimension.jaffle_store_name", "op": op}],
        }
        response = adapter.call_tool("execute", {"query": query, "mode": "validate"})
        issue = response["errors"][0]
        assert issue["code"] == "INVALID_EXPRESSION_AST"
        allowed = [
            "=",
            "!=",
            "<",
            "<=",
            ">",
            ">=",
            "IN",
            "NOT IN",
            "LIKE",
            "NOT LIKE",
            "IS NULL",
            "IS NOT NULL",
        ]
        assert issue["details"]["allowed"] == allowed
        hint = issue["recovery_hints"][0]
        assert hint["allowed"] == allowed
        assert "op 'IS NULL' or 'IS NOT NULL' and omit value" in hint["message"]
        assert "SQL" not in hint["message"]
        for null_op in ("IS NULL", "IS NOT NULL"):
            query["where"][0]["op"] = null_op
            assert adapter.call_tool("execute", {"query": query, "mode": "validate"})["ok"]
    finally:
        runtime.close()


def test_inline_prior_period_with_offset_alias_returns_authored_alternative(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # The reviewer's exact malformed shape — ``offset: {unit: "month", n: 1}``
        # used to bubble up as "expression must be an object" with no hint
        # because the IR-shape path recursed on a missing ``input``.
        result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "prior_period",
                            "measure": "measure.jaffle.revenue_usd",
                            "offset": {"unit": "month", "n": 1},
                        },
                        "as": "prev",
                    }
                ],
            }
        )
        errors = result.get("errors") or []
        assert errors, f"expected an INVALID_EXPRESSION_AST envelope, got {result!r}"
        first = errors[0]
        assert first["code"] == "INVALID_EXPRESSION_AST"
        details = first.get("details") or {}
        assert details.get("expression_kind") == "prior_period"
        assert "n" in details.get("unknown_offset_keys", [])
        # Authored counterpart suggestion is the headline fix — the
        # reviewer's "path forward is the authored measure (which is
        # the better answer anyway)".
        closest = details.get("closest_matches") or []
        assert any(cid.startswith("metric.") and "prior" in cid.lower() for cid in closest), (
            f"expected an authored prior-period metric in closest_matches, got {closest!r}"
        )
        hint_kinds = {hint.get("kind") for hint in (first.get("recovery_hints") or [])}
        assert "use_authored_prior_period_metric" in hint_kinds
        assert "fix_prior_period_offset_shape" in hint_kinds
        assert "use_prior_period_shorthand" in hint_kinds
    finally:
        runtime.close()


def test_path_not_found_envelope_carries_reachable_targets_and_hint(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # `customer_segment_membership` is modeled with `bridge: false`,
        # so order-grain measures can't reach its dimensions — a clean
        # PATH_NOT_FOUND scenario.
        result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "measure",
                            "measure": "measure.jaffle.revenue_usd",
                        },
                        "as": "rev",
                    }
                ],
                "group_by": ["dimension.jaffle_membership_status"],
            }
        )
        errors = result.get("errors") or []
        assert errors, f"expected PATH_NOT_FOUND envelope, got {result!r}"
        first = errors[0]
        assert first["code"] == "PATH_NOT_FOUND"
        details = first.get("details") or {}
        assert details.get("start") == "entity.jaffle_order"
        assert details.get("target") == "entity.jaffle_customer_segment_membership"
        # ``reachable_targets`` is always present (may be empty for
        # truly isolated entities). The recovery hints must be non-empty.
        assert "reachable_targets" in details
        assert details["target"] not in details["reachable_targets"]
        assert "dimension.jaffle_membership_status" not in details["compatible_group_by_dimensions"]
        hints = first.get("recovery_hints") or []
        assert hints, f"expected non-empty recovery_hints, got {first!r}"
        hint_kinds = {hint.get("kind") for hint in hints}
        # Either the "isolated source" branch (no reachable targets) or
        # the "use reachable target" branch (some reachable) — never neither.
        assert hint_kinds & {"isolated_source_entity", "use_reachable_target"}
        # The author-relationship escape hatch is always present.
        assert "author_relationship" in hint_kinds
    finally:
        runtime.close()


def test_catalog_minimal_returns_skeleton_rows_and_counts(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        minimal = catalog_payload(runtime, verbosity="minimal")
        # Skeleton rows only — id, kind, name, label, available.
        valid_keys = {"id", "kind", "name", "label", "available"}
        for bucket in ("measures", "metrics", "dimensions", "entities"):
            for row in minimal[bucket]:
                assert set(row.keys()).issubset(valid_keys), (
                    f"minimal {bucket} row has stray keys: {set(row.keys()) - valid_keys}"
                )
        # No alias dictionaries at minimal — agents inspect by id.
        assert "aliases" not in minimal
        assert "alias_index" not in minimal
        # Counts give agents the rough catalog shape without paying for the rows.
        assert "counts" in minimal
        assert minimal["counts"]["measures"] > 0
        # Sanity: minimal jaffle catalog fits in a 50KB envelope.
        size = len(json.dumps(minimal))
        assert size < 50_000, f"minimal catalog still too large: {size} bytes"
    finally:
        runtime.close()


def test_plan_minimal_strips_heavy_validation_payload(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        full = plan_candidate_envelope(
            runtime, intent="revenue this year", limit=3, verbosity="full"
        )
        minimal = plan_candidate_envelope(
            runtime, intent="revenue this year", limit=3, verbosity="minimal"
        )
        # Minimal must shrink the response materially.
        assert len(json.dumps(minimal)) < len(json.dumps(full)) // 2
        # Each candidate at minimal exposes the IR, confidence, and a
        # boolean ``validation_ok`` echo — not the full validation report.
        for cand in minimal["candidates"]:
            assert "candidate_ir" in cand
            assert "confidence" in cand
            assert "validation_ok" in cand
            assert "validation" not in cand
            assert "query_patch" not in cand
        # Sanity: minimal jaffle plan fits in a 5KB envelope.
        assert len(json.dumps(minimal)) < 5_000
    finally:
        runtime.close()


# ---------- Phase 1 — recovery-hint papercut tests ----------


def test_parameter_schema_disclosed_for_percentile_unknown_key(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # Reviewer's footgun: agent typo'd ``aggregation_params`` for
        # ``parameters``. The existing recovery hint told them to use
        # ``parameters`` but didn't reveal that ``parameters`` is
        # ``{p: float in [0, 1]}`` for percentile — two round-trips.
        result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "aggregate",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "percentile",
                            "aggregation_params": {"p": 0.9},
                        },
                        "as": "p90",
                    }
                ],
            }
        )
        errors = result.get("errors") or []
        assert errors, f"expected INVALID_EXPRESSION_KEY, got {result!r}"
        first = errors[0]
        assert first["code"] == "INVALID_EXPRESSION_KEY"
        hints = first.get("recovery_hints") or []
        schema_hints = [h for h in hints if h.get("kind") == "disclose_parameters_schema"]
        assert schema_hints, f"expected disclose_parameters_schema hint, got {hints!r}"
        schema = schema_hints[0].get("parameters_schema") or {}
        assert schema.get("p"), f"expected p key in parameters_schema, got {schema!r}"
    finally:
        runtime.close()


def test_cross_position_key_suggestion_on_order_by_with_dimension(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # The agent used ``dimension`` (the ``where`` canonical key)
        # inside an ``order_by`` entry. Recovery hint should tell them
        # the canonical key here is ``field`` and that ``dimension``
        # belongs in ``where``.
        result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "measure",
                            "measure": "measure.jaffle.order_count",
                        },
                        "as": "orders",
                    }
                ],
                "order_by": [{"dimension": "orders", "direction": "DESC"}],
            }
        )
        errors = result.get("errors") or []
        assert errors, f"expected INVALID_EXPRESSION, got {result!r}"
        first = errors[0]
        assert first["code"] == "INVALID_EXPRESSION"
        hints = first.get("recovery_hints") or []
        cross_hints = [h for h in hints if h.get("kind") == "use_canonical_key_for_position"]
        assert cross_hints, f"expected use_canonical_key_for_position hint, got {hints!r}"
        cross = cross_hints[0]
        assert cross.get("expected_key_here") == "field"
        assert cross.get("received_canonical_key") == "dimension"
        assert cross.get("received_canonical_key_belongs_to") == "where"
    finally:
        runtime.close()


def test_object_not_found_with_no_close_matches_routes_to_discover(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # The reviewer's complaint: ``measure.jaffle.nonexistent_measure``
        # returned three irrelevant matches (open_store_count_eop,
        # item_margin_usd, item_count). After the floor tightening,
        # ``zxqv_nonsense`` must return [] + a discover handoff.
        result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "measure",
                            "measure": "measure.jaffle.zxqv_nonsense",
                        },
                        "as": "x",
                    }
                ],
            }
        )
        errors = result.get("errors") or []
        assert errors, f"expected OBJECT_NOT_FOUND, got {result!r}"
        first = errors[0]
        assert first["code"] == "OBJECT_NOT_FOUND"
        closest = list(first.get("details", {}).get("closest_matches", []) or [])
        # No fuzzy match should meet the new floor for "zxqv_nonsense".
        assert closest == [], f"expected empty closest_matches, got {closest!r}"
        hints = first.get("recovery_hints") or []
        discover_hints = [h for h in hints if h.get("kind") == "call_discover_to_locate"]
        assert discover_hints, f"expected call_discover_to_locate hint, got {hints!r}"
        discover_term = discover_hints[0].get("recommended_terms", "")
        assert "zxqv" in discover_term or "nonsense" in discover_term
    finally:
        runtime.close()


# ---------- Round-three Phase 1 tests ----------


def test_unsupported_aggregation_discloses_parameter_schema(runtime_factory):
    """Percentile without ``parameters.p`` must surface
    ``parameters_schema`` so the agent learns the inner shape on the
    first error — symmetry with INVALID_EXPRESSION_KEY (round-two 1A)."""
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "aggregate",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "percentile",
                            # Note: ``parameters`` block is missing — the
                            # raise site at bind.py:_aggregation_expr
                            # surfaces the schema disclosure.
                        },
                        "as": "p",
                    }
                ],
            }
        )
        errors = result.get("errors") or []
        assert errors, f"expected UNSUPPORTED_AGGREGATION envelope, got {result!r}"
        first = errors[0]
        assert first["code"] == "UNSUPPORTED_AGGREGATION"
        hints = first.get("recovery_hints") or []
        disclose_hints = [h for h in hints if h.get("kind") == "disclose_parameters_schema"]
        assert disclose_hints, f"expected disclose_parameters_schema hint, got {hints!r}"
        assert disclose_hints[0].get("parameters_schema", {}).get("p")
        assert disclose_hints[0].get("aggregation") == "percentile"
    finally:
        runtime.close()


def test_path_not_found_envelope_lists_compatible_dimensions(runtime_factory):
    """PATH_NOT_FOUND must include concrete ``dimension.<id>`` ids the
    agent can paste into ``group_by`` directly."""
    runtime = runtime_factory("jaffle_shop")
    try:
        # `customer_segment_membership` is `bridge: false`, so order-grain
        # measures hit PATH_NOT_FOUND when grouped by its dimensions.
        result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "measure",
                            "measure": "measure.jaffle.revenue_usd",
                        },
                        "as": "rev",
                    }
                ],
                "group_by": ["dimension.jaffle_membership_status"],
            }
        )
        errors = result.get("errors") or []
        assert errors and errors[0]["code"] == "PATH_NOT_FOUND"
        details = errors[0].get("details") or {}
        # New envelope field — list of concrete dimension ids.
        assert "compatible_group_by_dimensions" in details
        # The hint surfaces them in the recovery_hints list too.
        hints = errors[0].get("recovery_hints") or []
        switch_dim = [h for h in hints if h.get("kind") == "switch_group_by_dimension"]
        # Empty list is OK if the start entity has no local dimensions —
        # but the hint structure must be present whenever the details
        # carries them.
        if details.get("compatible_group_by_dimensions"):
            assert switch_dim, f"expected switch_group_by_dimension hint, got {hints!r}"
    finally:
        runtime.close()


def test_dict_in_where_value_is_rejected_with_structured_envelope(runtime_factory):
    """Round-two Phase 2 widened ``metric_predicate.value`` to accept a
    percentile expression. ``where[].value`` was never widened — the
    9 other SqlLiteral wrap sites would silently stringify a dict.
    Round-three Phase 1E rejects this at validate-time with the right
    pointer to ``metric_filters``."""
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "measure",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                        },
                        "as": "rev",
                    }
                ],
                "where": [
                    {
                        "field": "dimension.jaffle_store_name",
                        "op": "=",
                        "value": {"kind": "percentile", "p": 0.9},
                    }
                ],
            }
        )
        errors = result.get("errors") or []
        assert errors, f"expected INVALID_QUERY envelope, got {result!r}"
        first = errors[0]
        assert first["code"] == "INVALID_QUERY"
        details = first.get("details") or {}
        assert "where" in details.get("path", "")
        # The hint must point the agent at metric_filters / scoped_aggregate
        # where dict-shaped values are accepted.
        assert "metric" in details.get("why_invalid", "").lower()
    finally:
        runtime.close()


# ---------- Round four — bloat / redundancy regressions ----------


def test_inspect_drops_preferred_alias_fields(runtime_factory):
    """``recommended_dimensions``/``recommended_filters`` (and their
    ``preferred_*`` aliases) were sourced from the ``related_dimensions``
    authoring field, which has been removed. The cards no longer emit any
    of these fields."""
    from semantic_rails.metadata import inspect_payload

    runtime = runtime_factory("jaffle_shop")
    try:
        measure_card = inspect_payload(runtime, object_id="measure.jaffle.order_count")["card"]
        metric_card = inspect_payload(runtime, object_id="metric.sales.aov_usd")["card"]
        for card, label in [(measure_card, "measure"), (metric_card, "metric")]:
            assert "recommended_dimensions" not in card, (
                f"{label} card still emits recommended_dimensions"
            )
            assert "recommended_filters" not in card, (
                f"{label} card still emits recommended_filters"
            )
            assert "preferred_dimensions" not in card, (
                f"{label} card still emits preferred_dimensions alias"
            )
            assert "preferred_filters" not in card, (
                f"{label} card still emits preferred_filters alias"
            )
    finally:
        runtime.close()


def test_plan_candidate_emits_only_candidate_ir(runtime_factory):
    """``candidate_ir``, ``query``, and ``query_patch`` were three keys
    holding the exact same dict on every planned candidate. They are now
    collapsed to a single canonical ``candidate_ir`` at every verbosity;
    compact also slims the validation report down to ok / errors / warnings."""
    runtime = runtime_factory("jaffle_shop")
    try:
        for verbosity in ("full", "compact"):
            result = plan_candidate_envelope(
                runtime, intent="revenue this year", limit=3, verbosity=verbosity
            )
            candidates = result.get("candidates") or []
            assert candidates, (
                f"{verbosity}: expected at least one candidate for the revenue intent"
            )
            for cand in candidates:
                assert "candidate_ir" in cand, f"{verbosity}: candidate missing candidate_ir"
                assert isinstance(cand["candidate_ir"], dict)
                assert cand["candidate_ir"].get("select"), (
                    f"{verbosity}: candidate_ir has no select"
                )
                assert "query" not in cand, (
                    f"{verbosity}: redundant ``query`` alias of candidate_ir"
                )
                assert "query_patch" not in cand, (
                    f"{verbosity}: redundant ``query_patch`` alias of candidate_ir"
                )
            if verbosity == "compact":
                for cand in candidates:
                    validation = cand.get("validation") or {}
                    # Heavy keys must be gone — explain / logical_plan / etc.
                    assert "explain" not in validation
                    assert "logical_plan" not in validation
                    assert "normalized_query" not in validation
                    # ok / errors / warnings remain so callers can still gate on validity.
                    assert "ok" in validation
    finally:
        runtime.close()


def test_catalog_compact_drops_alias_index_and_caps_rows(runtime_factory):
    """Compact catalog used to emit ``aliases`` + ``alias_index`` — both
    grew unbounded with the catalog and drowned token budgets even
    when narrowed with ``search=``. Round four moved those maps to
    ``verbosity=full`` and added a per-kind row cap so the compact
    payload stays bounded by construction."""
    from semantic_rails.metadata import _CATALOG_COMPACT_BUCKET_CAP

    runtime = runtime_factory("jaffle_shop")
    try:
        compact = catalog_payload(runtime, verbosity="compact")
        # Alias maps are gone — agents request ``verbosity=full`` if they
        # need them (typo-resolution etc.).
        assert "aliases" not in compact, "compact still emits aliases dict"
        assert "alias_index" not in compact, "compact still emits alias_index dict"
        # ``payload`` is the 19KB+/object source of catalog bloat — must
        # also be absent on measure/metric rows at compact.
        for row in compact["measures"]:
            assert "payload" not in row, f"compact measure carries payload: {row['id']}"
        for row in compact["metrics"]:
            assert "payload" not in row, f"compact metric carries payload: {row['id']}"
        # ``counts`` (per-bucket actually emitted) and ``counts_total``
        # (pre-cap totals) let agents see what got truncated.
        assert "counts" in compact and "counts_total" in compact
        for bucket_key, count in compact["counts"].items():
            assert count <= _CATALOG_COMPACT_BUCKET_CAP, (
                f"compact bucket {bucket_key} exceeded cap: {count}"
            )
            total = compact["counts_total"].get(bucket_key, 0)
            assert count <= total, f"compact bucket {bucket_key} emitted more rows than its total"
        # Full verbosity still ships the alias maps, uncapped — opt-in.
        full = catalog_payload(runtime, verbosity="full")
        assert "alias_index" in full and "aliases" in full
        assert "truncated" not in full, "full verbosity must never truncate"
    finally:
        runtime.close()


# ---------- Round five — orientation + windowed hints + naming ----------


def test_capabilities_payload_returns_under_5kb_envelope(runtime_factory):
    """The new ``capabilities`` MCP tool is the cheapest cold-start
    orientation call — under 5KB for jaffle_shop so an agent can ask
    'what can this package do?' without paying for a 227KB catalog."""
    from semantic_rails.metadata_parts.capabilities import capabilities_payload

    runtime = runtime_factory("jaffle_shop")
    try:
        payload = capabilities_payload(runtime)
        size = len(json.dumps(payload))
        assert size < 5_000, f"capabilities payload too large: {size} bytes"
        assert payload["package_id"] == "jaffle_shop"
        assert payload["schema_version"] >= 1
        kinds = {row["kind"] for row in payload["capabilities"]}
        # Sanity: a few canonical capabilities must be present.
        assert "rolling_windows" in kinds
        assert "metric_predicates" in kinds
        # Unsupported list exists even when empty (some packages have gaps).
        assert isinstance(payload["unsupported_capabilities"], list)
    finally:
        runtime.close()


def test_catalog_summary_returns_counts_plus_id_lists_under_10kb(runtime_factory):
    """``verbosity=summary`` returns counts + flat ID list per kind +
    capabilities. No row dicts — agents inspect specific ids by id."""
    runtime = runtime_factory("jaffle_shop")
    try:
        summary = catalog_payload(runtime, verbosity="summary")
        size = len(json.dumps(summary))
        assert size < 10_000, f"summary catalog too large: {size} bytes"
        # Singular id-list keys (entity_ids, not entitie_ids).
        for key in (
            "measure_ids",
            "metric_ids",
            "dimension_ids",
            "entity_ids",
            "segment_ids",
            "temporal_role_ids",
            "relationship_ids",
            "value_domain_ids",
        ):
            assert key in summary, f"summary missing {key}"
            assert isinstance(summary[key], list)
            for item in summary[key]:
                assert isinstance(item, str) and item, f"{key} has bad id"
        assert "counts" in summary
        assert "capabilities" in summary
        # Row dicts MUST be absent — that's the whole point of summary.
        for bucket in ("measures", "metrics", "dimensions", "entities"):
            assert bucket not in summary, (
                f"summary leaked row dicts under {bucket} — should be ids only"
            )
    finally:
        runtime.close()


def test_windowed_time_filter_unsupported_emits_widen_and_drop_hints(runtime_factory):
    """A bounded ``query.time.start`` against a prior_period / rolling
    metric used to ship the error with empty ``recovery_hints``. It now
    attaches two concrete patches the agent can mechanically apply:
    ``drop_time_start`` first, then ``widen_time_window`` with a computed
    ``suggested_start``."""
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {"metric": "metric.sales.prior_week_revenue_direct"},
                        "as": "v",
                    }
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "day",
                    "start": "2017-03-15",
                },
            }
        )
        errors = result.get("errors") or []
        assert errors, "expected a WINDOWED_TIME_FILTER_UNSUPPORTED envelope"
        first = errors[0]
        assert first["code"] == "WINDOWED_TIME_FILTER_UNSUPPORTED"
        # Lookback is attached so the enricher can compute the patch.
        lookback = first.get("details", {}).get("lookback") or {}
        assert lookback.get("unit") == "day"
        assert lookback.get("value", 0) >= 1
        # Both hints must be present.
        hints = list(first.get("recovery_hints") or [])
        hint_kinds = [h.get("kind") for h in hints]
        assert "widen_time_window" in hint_kinds
        assert "drop_time_start" in hint_kinds
        # ``drop_time_start`` is always safe; ``widen_time_window`` can still
        # fail for prior_period at month grain. Callers tend to apply the first
        # hint, so the safe one must come first.
        assert hint_kinds[0] == "drop_time_start", f"expected drop_time_start first, got {hints!r}"
        assert hint_kinds.index("widen_time_window") > 0
        widen = next(h for h in hints if h.get("kind") == "widen_time_window")
        # ``suggested_start`` is concrete: 2017-03-15 minus 7 days.
        assert widen["suggested_start"] == "2017-03-08"
    finally:
        runtime.close()


def test_empty_time_window_reports_diagnostics_warning_and_data_coverage(runtime_factory):
    """A valid query whose time window holds no data (jaffle data is
    2016-2017; the window is 2099) executes and returns zero rows. The
    result must say the data is missing rather than imply a query bug:
    ``data_diagnostics`` with a bounded ``requested_window`` and the root
    entity's ``actual_data_coverage`` (min/max of its time column), and an
    ``EMPTY_RESULT_WINDOW`` warning carrying the same fields for readers
    that only look at warnings."""
    runtime = runtime_factory("jaffle_shop")
    try:
        result = runtime.query(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "measure",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                        },
                        "as": "rev",
                    }
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "day",
                    "start": "2099-01-01",
                    "end": "2099-12-31",
                },
            }
        )
        assert result.get("row_count") == 0
        diag = result.get("data_diagnostics")
        assert diag, f"expected data_diagnostics on empty result, got keys={list(result.keys())}"
        assert diag.get("rows_returned") == 0
        assert diag.get("applied_time_filter", {}).get("start") == "2099-01-01"
        assert "inspect(" in diag.get("hint", ""), f"expected an inspect handoff, got {diag!r}"
        requested = diag.get("requested_window") or {}
        assert requested.get("start") == "2099-01-01"
        coverage = diag.get("actual_data_coverage") or {}
        assert coverage, "actual_data_coverage missing — probe failed silently"
        assert coverage.get("min"), "coverage min should be populated"
        assert coverage.get("max"), "coverage max should be populated"
        # Sanity: requested 2099 falls outside actual 2016-2017 coverage.
        assert coverage["min"] < "2099"
        warnings = result.get("warnings") or []
        empty_window = [w for w in warnings if w.get("code") == "EMPTY_RESULT_WINDOW"]
        assert empty_window, f"expected EMPTY_RESULT_WINDOW warning, got warnings={warnings!r}"
        details = empty_window[0].get("details", {})
        assert details.get("applied_time_filter", {}).get("start") == "2099-01-01"
        assert details.get("actual_data_coverage") == coverage
    finally:
        runtime.close()


def test_where_uses_field_key_and_rejects_legacy_dimension_key(runtime_factory):
    """Round five standardized on ``field`` for both ``where[]`` and
    ``order_by[]``. The legacy ``dimension`` key on ``where[]`` items
    is no longer accepted; the error points at ``where[N].field``."""
    runtime = runtime_factory("jaffle_shop")
    try:
        # Canonical shape: ``field`` works.
        ok_result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "measure",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                        },
                        "as": "rev",
                    }
                ],
                "where": [{"field": "dimension.jaffle_store_name", "op": "=", "value": "Brooklyn"}],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            }
        )
        assert ok_result.get("ok") is True, (
            f"field key must validate: errors={ok_result.get('errors')!r}"
        )
        # Legacy ``dimension`` key is now rejected with a clear error
        # pointing at the canonical key.
        bad_result = runtime.validate(
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "kind": "measure",
                            "measure": "measure.jaffle.revenue_usd",
                            "aggregation": "sum",
                        },
                        "as": "rev",
                    }
                ],
                "where": [
                    {"dimension": "dimension.jaffle_store_name", "op": "=", "value": "Brooklyn"}
                ],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            }
        )
        errors = bad_result.get("errors") or []
        assert errors, "legacy dimension key should fail validation"
        assert errors[0]["details"]["path"] == "where[0].field"
    finally:
        runtime.close()


@pytest.mark.parametrize("outer_verbosity", [None, "minimal", "compact"])
def test_cross_fact_rewrite_analysis_is_opt_in(runtime_factory, outer_verbosity):
    from semantic_rails.mcp import SemanticLayerMCPAdapter

    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    query = {
        "select": [
            {
                "expression": {
                    "measure": "measure.jaffle.order_count",
                    "aggregation": "count_distinct",
                },
                "as": "orders",
            },
            {
                "expression": {
                    "measure": "measure.jaffle.item_count",
                    "aggregation": "count_distinct",
                },
                "as": "items",
            },
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
    }
    try:
        responses = [
            adapter.call_tool(
                "execute",
                {
                    "query": {**query, "verbosity": verbosity},
                    "mode": "run",
                    "verbosity": outer_verbosity or verbosity,
                },
            )
            for verbosity in ("minimal", "compact", "full")
        ]
        minimal, compact, full = responses
        assert all(r["ok"] for r in responses)
        assert all("recovery_hints" not in r for r in responses)
        assert minimal["rows"] == compact["rows"] == full["rows"]
        rewrites = [[w for w in r["warnings"] if w["code"] == "REWRITE_APPLIED"] for r in responses]
        assert rewrites[0]
        assert rewrites[1] == rewrites[2]
        for small, verbose in zip(rewrites[0], rewrites[1], strict=True):
            assert verbose["details"]["analysis"]
            assert verbose["details"]["path"]
            assert small["code"] == verbose["code"] and small["message"] == verbose["message"]
            assert small["details"] == {
                key: value
                for key, value in verbose["details"].items()
                if key not in {"analysis", "path"}
            }
            assert "rewrite_kind" in small["details"]
    finally:
        adapter.close()
