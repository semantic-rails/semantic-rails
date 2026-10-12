"""Recovery hints, closest matches and payload sizes on the MCP surface."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import catalog_payload
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.plan_candidate_envelope import plan_candidate_envelope
from tests.semantic_rails.conftest import opened
from tests.semantic_rails.test_runtime_and_metadata import jaffle_snapshot as jaffle_snapshot


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

        def view_for(self, context):
            self.config_reads += 1
            return self.runtime.view_for(context) if self.config_reads == 1 else reloaded_config

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
                {"query": {"select": [missing_id]}, "mode": "sql", "verbosity": "full"},
                request_context=RequestContext(audience="external"),
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
def test_missing_counterpart_suggestions_follow_request_visibility(
    package_config_factory, missing_kind, mode, with_policy
):
    from semantic_rails.request_context import RequestContext

    config, package = package_config_factory("jaffle_shop")
    config, hidden_id, fuzzy_id = _counterpart_config(config, missing_kind, with_policy)
    runtime = Runtime.from_config(config, source_path=str(package))
    try:
        query = {"select": [f"{missing_kind}.synthetic.private_fee"]}
        response = SemanticLayerMCPAdapter(runtime).call_tool(
            "execute",
            {"query": query, "mode": mode, "verbosity": "full"},
            request_context=RequestContext(audience="external"),
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
            assert fuzzy_id not in json.dumps(response)
    finally:
        runtime.close()


@pytest.mark.parametrize("missing_kind", ["metric", "measure"])
@pytest.mark.parametrize("mode", ["run", "validate", "sql"])
def test_unavailable_visibility_cannot_add_a_counterpart(
    package_config_factory, monkeypatch, missing_kind, mode
):
    from semantic_rails import visible_view
    from semantic_rails.request_context import RequestContext

    config, package = package_config_factory("jaffle_shop")
    config, hidden_id, _ = _counterpart_config(config, missing_kind, True)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Visibility unavailable")

    monkeypatch.setattr(visible_view, "hidden_object_ids", unavailable)
    runtime = Runtime.from_config(config, source_path=str(package))
    try:
        response = SemanticLayerMCPAdapter(runtime).call_tool(
            "execute",
            {"query": {"select": [f"{missing_kind}.synthetic.private_fee"]}, "mode": mode},
            request_context=RequestContext(audience="external"),
        )
        # Unresolved visibility refuses before binding, naming nothing.
        assert response["errors"][0]["code"] == "POLICY_DENIED"
        assert response["errors"][0]["details"] == {"reason": "visibility_unresolved"}
        assert hidden_id not in json.dumps(response)
    finally:
        runtime.close()


@pytest.fixture(scope="module")
def runtime(jaffle_snapshot):
    runtime = opened(Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop"))
    yield runtime
    runtime.close()


def test_inline_prior_period_with_offset_alias_returns_authored_alternative(runtime):
    # ``n`` for the offset's ``value``: the hint names the authored prior-period metric.
    expression = {
        "kind": "prior_period",
        "input": "measure.jaffle.revenue_usd",
        "offset": {"unit": "month", "n": 1},
    }
    first = runtime.validate({"select": [{"expression": expression, "as": "prev"}]})["errors"][0]
    assert first["code"] == "INVALID_EXPRESSION_AST"
    details = first["details"]
    assert details["expression_kind"] == "prior_period"
    assert "n" in details["unknown_offset_keys"]
    assert any(cid.startswith("metric.") and "prior" in cid for cid in details["closest_matches"])
    hints = {hint["kind"]: hint for hint in first["recovery_hints"]}
    assert {"use_authored_prior_period_metric", "fix_prior_period_offset_shape"} <= set(hints)
    assert hints["use_prior_period_ir_shape"]["shape"]["input"] == "measure.jaffle.revenue_usd"


def test_path_not_found_envelope_carries_reachable_targets_and_hints(runtime):
    # Segment membership is modeled with `bridge: false`, so order-grain measures
    # can't reach its dimensions.
    (first,) = runtime.validate(
        {
            "select": [{"expression": "measure.jaffle.revenue_usd", "as": "rev"}],
            "group_by": ["dimension.jaffle_membership_status"],
        }
    )["errors"]
    assert first["code"] == "PATH_NOT_FOUND"
    details = first["details"]
    assert details["start"] == "entity.jaffle_order"
    assert details["target"] == "entity.jaffle_customer_segment_membership"
    assert details["target"] not in details["reachable_targets"]
    compatible = details["compatible_group_by_dimensions"]
    assert compatible and "dimension.jaffle_membership_status" not in compatible
    hint_kinds = {hint["kind"] for hint in first["recovery_hints"]}
    assert hint_kinds & {"isolated_source_entity", "use_reachable_target"}
    assert {"author_relationship", "switch_group_by_dimension"} <= hint_kinds


def test_catalog_minimal_returns_skeleton_rows_and_counts(runtime):
    minimal = catalog_payload(runtime, verbosity="minimal")
    for bucket in ("measures", "metrics", "dimensions", "entities"):
        for row in minimal[bucket]:
            assert set(row) <= {"id", "kind", "name", "label", "available"}, row
    assert "aliases" not in minimal and "alias_index" not in minimal
    assert minimal["counts"]["measures"] > 0
    assert len(json.dumps(minimal)) < 50_000


def test_plan_minimal_strips_heavy_validation_payload(runtime):
    full, minimal = (
        plan_candidate_envelope(runtime, intent="revenue this year", limit=3, verbosity=verbosity)
        for verbosity in ("full", "minimal")
    )
    assert len(json.dumps(minimal)) < min(len(json.dumps(full)) // 2, 5_000)
    for cand in minimal["candidates"]:
        assert {"candidate_ir", "confidence", "validation_ok"} <= set(cand)
        assert "validation" not in cand and "query_patch" not in cand


_P90 = {"kind": "aggregate", "measure": "measure.jaffle.revenue_usd", "aggregation": "percentile"}
_STORE = "dimension.jaffle_store_name"


@pytest.mark.parametrize(
    ("query", "path", "fix"),
    [
        (
            {"select": [{"expression": {**_P90, "aggregation_params": {"p": 0.9}}, "as": "p90"}]},
            "select[0].expression.aggregation_params",
            {"select": [{"expression": {**_P90, "parameters": {"p": 0.9}}, "as": "p90"}]},
        ),
        (
            {"select": ["measure.jaffle.order_count"], "order_by": [{"dimension": "orders"}]},
            "order_by[0].dimension",
            {
                "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}],
                "order_by": [{"field": "orders"}],
            },
        ),
        (
            {
                "select": ["measure.jaffle.order_count"],
                "where": [{"dimension": _STORE, "op": "is", "value": "Brooklyn"}],
            },
            "where[0].dimension",
            {
                "select": ["measure.jaffle.order_count"],
                "where": [{"field": _STORE, "op": "is", "value": "Brooklyn"}],
            },
        ),
        # A where value is a scalar or a list, never an expression stringified into SQL.
        (
            {
                "select": ["measure.jaffle.revenue_usd"],
                "where": [{"field": _STORE, "op": "is", "value": {"kind": "percentile", "p": 0.9}}],
            },
            "where[0].value",
            None,
        ),
    ],
    ids=["parameters", "order-by-field", "where-field", "where-expression-value"],
)
def test_misshaped_call_is_refused_at_its_path_with_one_hint(runtime, query, path, fix):
    first = runtime.validate(query)["errors"][0]
    assert (first["code"], first["details"]["path"]) == ("INVALID_QUERY", path)
    [hint] = first["recovery_hints"]
    assert hint["code"] == ("FIX_QUERY_CALL" if fix else "USE_FILTER_VALUE_SHAPE")
    if fix is not None:
        assert runtime.validate(fix)["ok"]


def test_object_not_found_with_no_close_matches_routes_to_discover(runtime):
    # No id is near enough to suggest: the hint hands off to discover instead.
    query = {"select": [{"expression": "measure.jaffle.zxqv_nonsense", "as": "x"}]}
    first = runtime.validate(query)["errors"][0]
    assert first["code"] == "OBJECT_NOT_FOUND"
    assert first["details"].get("closest_matches", []) == []
    discover = [h for h in first["recovery_hints"] if h["kind"] == "call_discover_to_locate"]
    assert {"zxqv", "nonsense"} & set(discover[0]["recommended_terms"].split())


def test_unsupported_aggregation_discloses_parameter_schema(runtime):
    # A percentile without ``parameters`` learns the inner shape on the first error.
    expression = {
        "kind": "aggregate",
        "measure": "measure.jaffle.revenue_usd",
        "aggregation": "percentile",
    }
    first = runtime.validate({"select": [{"expression": expression, "as": "p"}]})["errors"][0]
    assert first["code"] == "UNSUPPORTED_AGGREGATION"
    [disclose] = [h for h in first["recovery_hints"] if h["kind"] == "disclose_parameters_schema"]
    assert disclose["aggregation"] == "percentile"
    assert disclose["parameters_schema"]["p"]


def test_inspect_drops_preferred_alias_fields(runtime):
    from semantic_rails.metadata import inspect_payload

    for object_id in ("measure.jaffle.order_count", "metric.sales.aov_usd"):
        card = inspect_payload(runtime, object_id=object_id)["card"]
        for kind in ("recommended", "preferred"):
            assert f"{kind}_dimensions" not in card and f"{kind}_filters" not in card, object_id


def test_plan_candidate_emits_only_candidate_ir(runtime):
    # One canonical ``candidate_ir`` at every verbosity; compact slims the validation report.
    for verbosity in ("full", "compact"):
        result = plan_candidate_envelope(
            runtime, intent="revenue this year", limit=3, verbosity=verbosity
        )
        assert result["candidates"], verbosity
        for cand in result["candidates"]:
            assert cand["candidate_ir"]["select"], verbosity
            assert "query" not in cand and "query_patch" not in cand, verbosity
            if verbosity == "compact":
                validation = cand["validation"]
                assert "ok" in validation
                assert not {"explain", "logical_plan", "normalized_query"} & set(validation)


def test_catalog_compact_drops_alias_index_and_caps_rows(runtime):
    from semantic_rails.catalog_format import _CATALOG_COMPACT_BUCKET_CAP

    compact = catalog_payload(runtime, verbosity="compact")
    assert "aliases" not in compact and "alias_index" not in compact
    for row in [*compact["measures"], *compact["metrics"]]:
        assert "payload" not in row, row["id"]
    for bucket, count in compact["counts"].items():
        assert count <= min(_CATALOG_COMPACT_BUCKET_CAP, compact["counts_total"][bucket]), bucket
    # Full verbosity keeps the alias maps, uncapped.
    full = catalog_payload(runtime, verbosity="full")
    assert "alias_index" in full and "aliases" in full
    assert "truncated" not in full


def test_capabilities_payload_returns_under_5kb_envelope(runtime):
    from semantic_rails.metadata_parts.capabilities import capabilities_payload

    payload = capabilities_payload(runtime)
    assert len(json.dumps(payload)) < 5_000
    assert payload["package_id"] == "jaffle_shop"
    assert payload["schema_version"] >= 1
    assert {"rolling_windows", "metric_predicates"} <= {
        row["kind"] for row in payload["capabilities"]
    }
    assert isinstance(payload["unsupported_capabilities"], list)


def test_catalog_summary_returns_counts_plus_id_lists_under_10kb(runtime):
    summary = catalog_payload(runtime, verbosity="summary")
    assert len(json.dumps(summary)) < 10_000
    for kind in (
        "measure",
        "metric",
        "dimension",
        "entity",
        "segment",
        "temporal_role",
        "relationship",
        "value_domain",
    ):
        assert all(isinstance(item, str) and item for item in summary[f"{kind}_ids"]), kind
    assert "counts" in summary and "capabilities" in summary
    assert not {"measures", "metrics", "dimensions", "entities"} & set(summary)


def test_windowed_time_filter_unsupported_emits_widen_and_drop_hints(runtime):
    # A bounded start on a prior-period metric: the safe hint (drop the start) comes first.
    query = {
        "select": [{"expression": "metric.sales.prior_week_revenue_direct", "as": "v"}],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "day",
            "start": "2017-03-15",
        },
    }
    first = runtime.validate(query)["errors"][0]
    assert first["code"] == "WINDOWED_TIME_FILTER_UNSUPPORTED"
    lookback = first["details"]["lookback"]
    assert lookback["unit"] == "day" and lookback["value"] >= 1
    hints = first["recovery_hints"]
    assert [h["kind"] for h in hints][:1] == ["drop_time_start"]
    widen = next(h for h in hints if h["kind"] == "widen_time_window")
    assert widen["suggested_start"] == "2017-03-08"


def test_empty_time_window_reports_diagnostics_warning_and_data_coverage(runtime):
    # The data is 2016-2017: an open window from 2099 says the data is missing.
    measure = {"kind": "measure", "measure": "measure.jaffle.revenue_usd", "aggregation": "sum"}
    window = {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"}
    result = runtime.query(
        {
            "select": [{"expression": measure, "as": "rev"}],
            "time": {**window, "start": "2099-01-01"},
        }
    )
    assert result["row_count"] == 0
    diag = result["data_diagnostics"]
    assert diag["rows_returned"] == 0
    assert diag["applied_time_filter"]["start"] == diag["requested_window"]["start"] == "2099-01-01"
    assert "inspect(" in diag["hint"]
    coverage = diag["actual_data_coverage"]
    assert coverage["max"] and coverage["min"] < "2099"
    (empty,) = [w for w in result["warnings"] if w["code"] == "EMPTY_RESULT_WINDOW"]
    assert empty["details"]["applied_time_filter"]["start"] == "2099-01-01"
    assert empty["details"]["actual_data_coverage"] == coverage


def test_cross_fact_rewrite_analysis_is_opt_in(runtime_factory):
    from semantic_rails.mcp import SemanticLayerMCPAdapter

    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    query = {
        "select": [
            {
                "expression": {
                    "kind": "measure",
                    "measure": "measure.jaffle.order_count",
                    "aggregation": "count_distinct",
                },
                "as": "orders",
            },
            {
                "expression": {
                    "kind": "measure",
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
            adapter.call_tool("execute", {"query": query, "mode": "run", "verbosity": verbosity})
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
