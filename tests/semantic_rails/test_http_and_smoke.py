from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import HTTPServer

import pytest

from semantic_rails.api import AppState, Handler
from semantic_rails.asgi import PackageLoadFailureApp, SemanticLayerASGIApp
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.request_context import (
    HeaderPolicyContextResolver,
    RequestContext,
    api_key_auth_result,
    set_policy_context_resolver,
)


def _fake_query_result(payload):
    group_by = list(payload.get("group_by", []) or [])
    dimension = group_by[0] if group_by else "dimension.jaffle_store_name"
    return {
        "rows": [
            {
                dimension: "Brooklyn",
                "orders": 42,
                "aov_usd": 19.5,
            }
        ],
        "row_count": 1,
        "rendered_sql": "select 1",
        "logical_plan": {},
        "sql_plan": {},
        "explain": {},
        "query": dict(payload),
        "normalized_query": {},
    }


PUBLIC_V1_ROUTE_INDEX = [
    {"method": "GET", "path": "/api/v1/health"},
    {"method": "GET", "path": "/api/v1/ready"},
    {"method": "GET", "path": "/api/v1/capabilities"},
    {"method": "GET", "path": "/api/v1/catalog"},
    {"method": "POST", "path": "/api/v1/catalog"},
    {"method": "POST", "path": "/api/v1/discover"},
    {"method": "POST", "path": "/api/v1/inspect"},
    {"method": "POST", "path": "/api/v1/build-options"},
    {"method": "POST", "path": "/api/v1/valid-values"},
    {"method": "POST", "path": "/api/v1/plan"},
    {"method": "POST", "path": "/api/v1/validate"},
    {"method": "POST", "path": "/api/v1/compile"},
    {"method": "POST", "path": "/api/v1/query"},
    {"method": "POST", "path": "/api/v1/segment-validate"},
    {"method": "POST", "path": "/api/v1/segment-explain"},
    {"method": "POST", "path": "/api/v1/segment-preview"},
]


def _read_json_response(resp) -> tuple[dict, object]:
    return dict(json.loads(resp.read().decode("utf-8")) or {}), resp.headers


def _get_response(url: str, headers: dict | None = None) -> tuple[dict, object]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req) as resp:  # nosec - local test server only
        return _read_json_response(resp)


def _get(url: str) -> dict:
    payload, _ = _get_response(url)
    return payload


def _get_error(url: str) -> tuple[int, dict]:
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get_response(url)
    error = exc_info.value
    payload = dict(json.loads(error.read().decode("utf-8")) or {})
    return error.code, payload


def _post_response(url: str, payload: dict, headers: dict | None = None) -> tuple[dict, object]:
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:  # nosec - local test server only
        return _read_json_response(resp)


def _post_raw_response(url: str, raw: bytes, headers: dict | None = None) -> tuple[dict, object]:
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    req = urllib.request.Request(
        url,
        data=raw,
        headers=request_headers,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:  # nosec - local test server only
        return _read_json_response(resp)


def _post(url: str, payload: dict) -> dict:
    response, _ = _post_response(url, payload)
    return response


@contextmanager
def _serve_runtime(runtime, package_id: str = "jaffle_shop"):
    state = AppState(package_id)
    state.runtime.close()
    state.runtime = runtime

    class _Handler(Handler):
        pass

    _Handler.state = state
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        state.runtime.close()


def test_api_v1_health_contract_envelope_and_request_id(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    with _serve_runtime(runtime) as base:
        payload, headers = _get_response(
            base + "/api/v1/health", headers={"X-Request-ID": "contract-req-1"}
        )

        assert headers["X-Request-ID"] == "contract-req-1"
        assert payload["ok"] is True
        assert payload["status"] == "ok"
        assert payload["api_version"] == "v1"
        assert payload["request_id"] == "contract-req-1"
        assert payload["package_id"] == "jaffle_shop"
        assert payload["package"]["id"] == "jaffle_shop"
        assert payload["service"] == "semantic-rails"
        assert payload["warnings"] == []
        assert payload["errors"] == []
        assert payload["recovery_hints"] == []
        assert isinstance(payload["timing_ms"], (int, float))


def test_api_v1_ready_contract_is_lightweight(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    with _serve_runtime(runtime) as base:
        payload = _get(base + "/api/v1/ready")

        assert payload["ok"] is True
        assert payload["status"] == "ok"
        assert payload["service"] == "semantic-rails"
        assert payload["warehouse"] == "duckdb"
        assert payload["checks"][0]["name"] == "package_loaded"


def test_api_v1_capabilities_exposes_locked_public_index(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    with _serve_runtime(runtime) as base:
        payload = _get(base + "/api/v1/capabilities")

        assert payload["ok"] is True
        assert payload["api_version"] == "v1"
        assert payload["supported_api_versions"] == ["v1"]
        assert payload["route_prefix"] == "/api/v1"
        assert "alias_prefixes" not in payload
        assert payload["routes"] == PUBLIC_V1_ROUTE_INDEX
        assert payload["package"]["id"] == "jaffle_shop"
        assert payload["schema_version"] == 1
        assert payload["capabilities"]
        assert "qualify" in payload["warehouse_capabilities"]


def test_public_http_contract_rejects_root_and_api_aliases(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    runtime.query = _fake_query_result

    with _serve_runtime(runtime) as base:
        root_status, root_payload = _get_error(base + "/catalog?verbosity=full")
        api_status, api_payload = _get_error(base + "/api/catalog?verbosity=full")
        v1_catalog, catalog_headers = _get_response(
            base + "/api/v1/catalog?verbosity=full&request_id=query-req"
        )

        assert root_status == 404
        assert api_status == 404
        assert root_payload["error"]["code"] == "NOT_FOUND"
        assert api_payload["error"]["code"] == "NOT_FOUND"
        assert catalog_headers["X-Request-ID"] == "query-req"
        assert v1_catalog["request_id"] == "query-req"
        assert "dimensionjafflestorename" in v1_catalog["catalog"]["alias_index"]

        queried, query_headers = _post_response(
            base + "/api/v1/query",
            {
                "request_id": "body-req",
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.order_count",
                            "aggregation": "count_distinct",
                        },
                        "as": "orders",
                    }
                ],
                "group_by": ["dimension.jaffle_store_name"],
            },
        )

        assert query_headers["X-Request-ID"] == "body-req"
        assert queried["request_id"] == "body-req"
        assert queried["api_version"] == "v1"
        assert queried["status"] == "ok"
        assert queried["package_id"] == "jaffle_shop"
        assert queried["warnings"] == []
        assert queried["errors"] == []
        assert queried["recovery_hints"] == []
        assert queried["query"]["group_by"] == ["dimension.jaffle_store_name"]
        assert "request_id" not in queried["query"]


def test_api_key_shim_and_trusted_request_context_headers(runtime_factory, monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_API_KEYS", "test-key")
    runtime = runtime_factory("jaffle_shop")
    with _serve_runtime(runtime) as base:
        health = _get(base + "/api/v1/health")
        assert health["ok"] is True

        ready = _get(base + "/api/v1/ready")
        assert ready["ok"] is True

        try:
            _get(base + "/api/v1/catalog")
            raise AssertionError("expected unauthorized catalog request")
        except urllib.error.HTTPError as exc:
            payload = dict(json.loads(exc.read().decode("utf-8")) or {})
            assert exc.code == 401
            assert payload["errors"][0]["code"] == "UNAUTHORIZED"

        try:
            _get_response(base + "/api/v1/ready", headers={"X-Semantic-Check-Warehouse": "true"})
            raise AssertionError("expected unauthorized deep readiness request")
        except urllib.error.HTTPError as exc:
            payload = dict(json.loads(exc.read().decode("utf-8")) or {})
            assert exc.code == 401
            assert payload["errors"][0]["code"] == "UNAUTHORIZED"

        deep_ready, _ = _get_response(
            base + "/api/v1/ready",
            headers={"Authorization": "Bearer test-key", "X-Semantic-Check-Warehouse": "true"},
        )
        assert deep_ready["ok"] is True
        assert any(check["name"] == "warehouse" for check in deep_ready["checks"])

        payload, _ = _post_response(
            base + "/api/v1/validate",
            {
                "query": {
                    "version": 1,
                    "select": [
                        {
                            "expression": {
                                "measure": "measure.jaffle.order_count",
                                "aggregation": "count_distinct",
                            },
                            "as": "orders",
                        }
                    ],
                    "policy_context": {"actor": "spoofed_body", "environment": "development"},
                },
                "policy_context": {"tenant": "spoofed_tenant", "audience": "body_audience"},
            },
            headers={
                "Authorization": "Bearer test-key",
                "X-Semantic-Actor": "user_123",
                "X-Semantic-Tenant": "tenant_a",
                "X-Semantic-Project": "project_alpha",
                "X-Semantic-Roles": "analyst,finance",
                "X-Semantic-Environment": "production",
                "X-Semantic-Audience": "executive",
            },
        )

        assert payload["ok"] is True
        assert payload["request_context"]["actor"] == "user_123"
        assert payload["request_context"]["tenant"] == "tenant_a"
        assert payload["request_context"]["project"] == "project_alpha"
        assert payload["request_context"]["roles"] == ["analyst", "finance"]
        assert payload["request_context"]["environment"] == "production"
        assert payload["query"]["policy_context"]["audience"] == "executive"
        assert payload["query"]["policy_context"]["actor"] == "user_123"
        assert payload["query"]["policy_context"]["tenant"] == "tenant_a"
        assert payload["query"]["policy_context"]["environment"] == "production"


def test_api_options_and_auth_boundary_are_browser_safe(runtime_factory, monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_API_KEYS", "test-key")
    runtime = runtime_factory("jaffle_shop")
    with _serve_runtime(runtime) as base:
        req = urllib.request.Request(base + "/api/v1/query", method="OPTIONS")
        with urllib.request.urlopen(req) as resp:  # nosec - local test server only
            assert resp.status == 204
            assert resp.headers["Content-Length"] == "0"
            assert "X-Semantic-API-Key" in resp.headers["Access-Control-Allow-Headers"]
            assert resp.read() == b""

        try:
            _post_raw_response(base + "/api/v1/query", b"{not-json")
            raise AssertionError("expected unauthorized protected POST before JSON parsing")
        except urllib.error.HTTPError as exc:
            payload = dict(json.loads(exc.read().decode("utf-8")) or {})
            assert exc.code == 401
            assert payload["errors"][0]["code"] == "UNAUTHORIZED"


def test_api_malformed_request_shapes_return_400_envelopes(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    with _serve_runtime(runtime) as base:
        for raw_payload in (
            {"query": "not-an-object"},
            {"query": {"version": 1, "policy_context": "not-an-object"}},
        ):
            try:
                _post_response(
                    base + "/api/v1/validate",
                    raw_payload,
                    headers={"X-Request-ID": "bad-query-shape"},
                )
                raise AssertionError("expected invalid request response")
            except urllib.error.HTTPError as exc:
                payload = dict(json.loads(exc.read().decode("utf-8")) or {})
                assert exc.code == 400
                assert payload["status"] == "error"
                assert payload["request_id"] == "bad-query-shape"
                assert payload["errors"][0]["code"] == "INVALID_REQUEST"

        try:
            _post_response(base + "/api/v1/discover", {"terms": "orders", "limit": "many"})
            raise AssertionError("expected invalid limit response")
        except urllib.error.HTTPError as exc:
            payload = dict(json.loads(exc.read().decode("utf-8")) or {})
            assert exc.code == 400
            assert payload["errors"][0]["code"] == "INVALID_REQUEST"
            assert "limit" in payload["errors"][0]["message"]


def test_api_valid_values_string_false_include_counts_matches_boolean(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    runtime.query = _fake_query_result
    with _serve_runtime(runtime) as base:
        payload = _post(
            base + "/api/v1/valid-values",
            {
                "dimension_id": "dimension.jaffle_order_customer_id",
                "query": {"version": 1},
                "allow_live_query": True,
                "include_counts": "false",
                "limit": 1,
            },
        )

        assert payload["ok"] is True
        assert payload["value_source_type"] == "live_query"
        assert payload["values"]
        assert "count" not in payload["values"][0]


def test_api_string_boolean_include_blocked_matches_json_boolean(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    with _serve_runtime(runtime) as base:
        query = {
            "version": 1,
            "select": [
                {
                    "expression": {
                        "measure": "measure.jaffle.order_count",
                        "aggregation": "count_distinct",
                    },
                    "as": "orders",
                }
            ],
        }
        json_false = _post(
            base + "/api/v1/build-options",
            {"query": query, "focus_terms": "orders by store", "include_blocked": False},
        )
        string_false = _post(
            base + "/api/v1/build-options",
            {"query": query, "focus_terms": "orders by store", "include_blocked": "false"},
        )

        assert string_false["blocked"] == json_false["blocked"] == []


def test_missing_api_key_file_keeps_auth_enabled(monkeypatch, tmp_path):
    missing = tmp_path / "missing.keys"
    monkeypatch.delenv("SEMANTIC_RAILS_API_KEYS", raising=False)
    monkeypatch.setenv("SEMANTIC_RAILS_API_KEY_FILE", str(missing))

    ok, reason = api_key_auth_result({"Authorization": "Bearer __missing_api_key_file__"})

    assert ok is False
    assert reason == "missing_or_invalid"


def test_api_key_check_uses_constant_time_compare(monkeypatch):
    """API key comparison must use hmac.compare_digest, not `in set(...)`.

    Hosted deployments are vulnerable to timing attacks when comparing
    supplied bearer tokens with `==` or set membership. This test asserts
    the implementation path goes through `hmac.compare_digest` and that
    valid keys match while close-but-different keys do not.
    """
    import semantic_rails.request_context as rc

    monkeypatch.setenv(
        "SEMANTIC_RAILS_API_KEYS",
        "real-secret-key-aaaaaaaa,another-real-key-bbbbbbbb",
    )
    monkeypatch.delenv("SEMANTIC_RAILS_API_KEY_FILE", raising=False)

    # Sanity: a valid key matches.
    ok, reason = api_key_auth_result({"Authorization": "Bearer real-secret-key-aaaaaaaa"})
    assert ok is True
    assert reason == "matched"

    ok, reason = api_key_auth_result({"X-API-Key": "another-real-key-bbbbbbbb"})
    assert ok is True
    assert reason == "matched"

    # A close prefix must not match (and must go through compare_digest).
    calls: list[tuple[bytes, bytes]] = []
    real_compare = rc.hmac.compare_digest

    def spy_compare(a, b):
        calls.append(
            (
                a if isinstance(a, bytes) else a.encode("utf-8"),
                b if isinstance(b, bytes) else b.encode("utf-8"),
            )
        )
        return real_compare(a, b)

    monkeypatch.setattr(rc.hmac, "compare_digest", spy_compare)

    ok, reason = api_key_auth_result({"Authorization": "Bearer real-secret-key-aaaaaaab"})
    assert ok is False
    assert reason == "missing_or_invalid"
    # Walked the full key list (no short-circuit on first mismatch).
    assert len(calls) >= 2, "expected hmac.compare_digest called for every configured key"

    # Empty supplied value is rejected without comparing.
    calls.clear()
    ok, reason = api_key_auth_result({})
    assert ok is False
    assert reason == "missing_or_invalid"
    assert calls == []


def test_metadata_endpoints_and_ui_smoke_path(runtime_factory, package_config_factory, monkeypatch):
    runtime = runtime_factory("jaffle_shop")
    _, alt_config_path = package_config_factory("jaffle_shop")
    monkeypatch.setattr(
        "semantic_rails.config.list_package_paths",
        lambda: {"jaffle_shop": str(alt_config_path)},
    )
    state = AppState("jaffle_shop")
    state.runtime.close()
    state.runtime = runtime
    state.runtime.query = _fake_query_result

    class _Handler(Handler):
        pass

    _Handler.state = state
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        catalog = _get(base + "/api/v1/catalog?verbosity=full")
        discovered = _post(
            base + "/api/v1/discover", {"terms": "drinks", "stage": "initial", "limit": 3}
        )
        inspected = _post(
            base + "/api/v1/inspect",
            {"object_id": "measure.jaffle.order_count", "verbosity": "compact"},
        )
        delivered_inspected = _post(
            base + "/api/v1/inspect",
            {"object_id": "measure.jaffle.delivered_revenue_usd", "verbosity": "compact"},
        )
        build_options = _post(
            base + "/api/v1/build-options",
            {
                "query": {
                    "version": 1,
                    "select": [
                        {
                            "expression": {
                                "measure": "measure.jaffle.order_count",
                                "aggregation": "count_distinct",
                            },
                            "as": "orders",
                        }
                    ],
                },
                "focus_terms": "orders by store",
                "focus_object_id": "measure.jaffle.order_count",
                "stage": "post_measure",
                "limit": 5,
            },
        )
        delivered_time_options = _post(
            base + "/api/v1/build-options",
            {
                "query": {
                    "version": 1,
                    "select": [
                        {
                            "expression": {"measure": "measure.jaffle.delivered_revenue_usd"},
                            "as": "Delivered revenue",
                        }
                    ],
                },
                "focus_terms": "delivered revenue by delivered month",
                "focus_object_id": "measure.jaffle.delivered_revenue_usd",
                "step": "time",
                "limit": 5,
            },
        )
        valid_values = _post(
            base + "/api/v1/valid-values",
            {
                "dimension_id": "dimension.jaffle_item_product_type",
                "search": "drink",
                "limit": 1,
            },
        )
        planned = _post(base + "/api/v1/plan", {"intent": "new customer orders over time"})
        comparison = _post(
            base + "/api/v1/plan", {"intent": "food vs drink revenue share by month"}
        )
        ordered_vs_delivered = _post(
            base + "/api/v1/plan", {"intent": "ordered vs delivered revenue by month"}
        )
        conversion_plan = _post(
            base + "/api/v1/plan", {"intent": "session to order conversion rate"}
        )
        segment_validated = _post(
            base + "/api/v1/segment-validate",
            {"segment_id": "segment.jaffle.high_value_customers"},
        )
        segment_explained = _post(
            base + "/api/v1/segment-explain",
            {"segment_id": "segment.jaffle.high_value_customers"},
        )
        segment_preview = _post(
            base + "/api/v1/segment-preview",
            {"segment_id": "segment.jaffle.high_value_customers", "limit": 3},
        )
        validated = _post(
            base + "/api/v1/validate",
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.order_count",
                            "aggregation": "count_distinct",
                        },
                        "as": "orders",
                    },
                    {"expression": {"metric": "metric.sales.aov_usd"}, "as": "aov_usd"},
                ],
                "group_by": ["dimension.jaffle_store_name"],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            },
        )
        compiled = _post(
            base + "/api/v1/compile",
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.order_count",
                            "aggregation": "count_distinct",
                        },
                        "as": "orders",
                    },
                    {"expression": {"metric": "metric.sales.aov_usd"}, "as": "aov_usd"},
                ],
                "group_by": ["dimension.jaffle_store_name"],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            },
        )
        queried = _post(
            base + "/api/v1/query",
            {
                "version": 1,
                "select": [
                    {
                        "expression": {
                            "measure": "measure.jaffle.order_count",
                            "aggregation": "count_distinct",
                        },
                        "as": "orders",
                    },
                    {"expression": {"metric": "metric.sales.aov_usd"}, "as": "aov_usd"},
                ],
                "group_by": ["dimension.jaffle_store_name"],
                "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
                "limit": 2,
            },
        )

        assert catalog["ok"] is True
        assert "dimensionjafflestorename" in catalog["catalog"]["alias_index"]
        assert (
            "dimension.jaffle_store_name"
            in catalog["catalog"]["alias_index"]["dimensionjafflestorename"]
        )
        assert catalog["catalog"]["meta"]["verbosity"] == "full"
        assert discovered["stage"] == "initial"
        assert discovered["measures"][0]["object_type"] == "measure"
        assert discovered["measures"]
        assert discovered["dimension_values"]
        assert inspected["card"]["usage_summary"]["default_aggregation"] == "count_distinct"
        # `default_metric_id` reports the auto-publish derivation pattern
        # (`metric.<ns>.<key>`) for clients that still expect the legacy
        # measure→metric convention. Auto-publish itself is gone in v1,
        # but the field name is preserved for backwards compatibility.
        assert inspected["card"]["default_metric_id"] == "metric.jaffle.order_count"
        assert delivered_inspected["card"]["usage_summary"]["default_aggregation"] == "sum"
        assert build_options["stage"] == "post_measure"
        assert build_options["builder_step"] == "group_by"
        assert build_options["recommended"][0]["id"] == "dimension.jaffle_store_name"
        assert (
            delivered_time_options["recommended"][0]["id"]
            == "temporal_role.jaffle_lifecycle_delivered_at"
        )
        assert delivered_time_options["recommended"][0]["query_patch"]["time"] == {
            "temporal_role": "temporal_role.jaffle_lifecycle_delivered_at",
            "grain": "month",
        }
        assert valid_values["source"] == "value_domain"
        assert valid_values["value_source_type"] == "declared_domain"
        assert valid_values["estimated_cost"] == "none"
        assert (
            valid_values["selection_context"]["dimension_id"]
            == "dimension.jaffle_item_product_type"
        )
        assert valid_values["values"][0]["value"] == "beverage"
        assert planned["status"] == "ok"
        assert "query_ir" in planned["best"]
        assert comparison["best"]
        assert ordered_vs_delivered["best"]
        assert conversion_plan["status"] == "ok"
        assert conversion_plan["best"]["validation_ok"] is True
        assert (
            segment_validated["normalized_segment"]["id"] == "segment.jaffle.high_value_customers"
        )
        assert segment_validated["derived_query"]["group_by"][0] == "dimension.jaffle_customer_id"
        assert segment_explained["segment"]["id"] == "segment.jaffle.high_value_customers"
        # Segment SQL should reference the lifetime-spend measure that the
        # segment's metric_predicate filter pulls from. The exact CTE shape
        # depends on the predicate plumbing; the surface contract is just
        # that the rendered SQL is non-empty and references the cohort key.
        assert "jaffle_customer.customer_id" in segment_explained["rendered_sql"]
        assert segment_preview["member_count"] >= segment_preview["preview_row_count"]
        assert segment_preview["rows"]
        assert "__segment_basis" not in segment_preview["rows"][0]
        assert validated["ok"] is True
        assert validated["query"]["group_by"] == ["dimension.jaffle_store_name"]
        assert (
            validated["normalized_query"]["time"]["temporal_role"]
            == "temporal_role.jaffle_order_time"
        )
        assert "leaf_1 AS (" in compiled["rendered_sql"]
        assert "projected AS (" not in compiled["rendered_sql"]
        assert compiled["query"]["group_by"] == ["dimension.jaffle_store_name"]
        assert queried["row_count"] > 0
        assert "logical_plan" in queried
        assert "sql_plan" in queried
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        state.runtime.close()


def test_valid_values_http_route_requires_explicit_live_lookup(runtime_factory):
    runtime = runtime_factory("jaffle_shop")

    def _raise_live_query(*args, **kwargs):
        raise AssertionError("valid-values should not query by default")

    runtime.query = _raise_live_query
    runtime._get_adapter = _raise_live_query

    with _serve_runtime(runtime) as base:
        default_payload = _post(
            base + "/api/v1/valid-values",
            {
                "dimension_id": "dimension.jaffle_order_customer_id",
                "query": {"version": 1},
            },
        )

        runtime.query = _fake_query_result
        live_payload = _post(
            base + "/api/v1/valid-values",
            {
                "dimension_id": "dimension.jaffle_order_customer_id",
                "query": {"version": 1},
                "allow_live_query": True,
            },
        )

        assert default_payload["ok"] is True
        assert default_payload["source"] == "none"
        assert default_payload["values"] == []
        assert default_payload["value_source_type"] == "none"
        assert live_payload["ok"] is True
        assert live_payload["source"] == "duckdb"
        assert live_payload["value_source_type"] == "live_query"
        assert live_payload["values"]


def test_query_route_returns_structured_error_on_unexpected_runtime_failure(
    runtime_factory, package_config_factory, monkeypatch
):
    runtime = runtime_factory("jaffle_shop")
    _, alt_config_path = package_config_factory("jaffle_shop")
    monkeypatch.setattr(
        "semantic_rails.config.list_package_paths",
        lambda: {"jaffle_shop": str(alt_config_path)},
    )
    state = AppState("jaffle_shop")
    state.runtime.close()
    state.runtime = runtime

    def _boom(payload):
        raise RuntimeError("boom")

    state.runtime.query = _boom

    class _Handler(Handler):
        pass

    _Handler.state = state
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        req = urllib.request.Request(
            base + "/api/v1/query",
            data=json.dumps(
                {
                    "version": 1,
                    "select": [
                        {
                            "expression": {
                                "measure": "measure.jaffle.order_count",
                                "aggregation": "count_distinct",
                            },
                            "as": "orders",
                        }
                    ],
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)  # nosec - local test server only
            raise AssertionError("expected HTTPError")
        except urllib.error.HTTPError as exc:  # nosec - local test server only
            payload = dict(json.loads(exc.read().decode("utf-8")) or {})
            assert exc.code == 500
            assert payload["ok"] is False
            assert payload["error"]["code"] == "INTERNAL_ERROR"
            assert "boom" in payload["error"]["message"]
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        state.runtime.close()


# ----------------------------------------------------------------------
# ASGI transport — the same HTTP envelope contract delivered over raw
# ASGI. The bulk of the contract is exercised above via the HTTPServer
# Handler. The two assertions below cover behaviors that are uniquely
# ASGI-specific:
#   1. lifespan protocol — startup/shutdown messages must be emitted
#      cleanly, including when package load has failed (so uvicorn
#      keeps the worker alive and routes to the 503 envelope).
#   2. PackageLoadFailureApp 503 — the fail-closed app must serve a
#      structured 503 envelope, not crash on lifespan.
# ----------------------------------------------------------------------


async def _asgi_call(
    app,
    *,
    method: str = "GET",
    path: str = "/api/v1/health",
    payload: dict | None = None,
    query_string: str = "",
):
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else b""
    sent: list = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.request", "body": b"", "more_body": False}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query_string.encode("utf-8"),
            "headers": [],
        },
        receive,
        send,
    )
    start_message = next(m for m in sent if m["type"] == "http.response.start")
    response_body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    parsed = json.loads(response_body.decode("utf-8")) if response_body else {}
    return start_message["status"], parsed


async def _asgi_lifespan_call(app):
    sent: list = []
    messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)
    return sent


def test_asgi_resolves_policy_context_once_for_valid_post() -> None:
    class CountingResolver:
        def __init__(self):
            self.calls = []

        def resolve(self, headers, *, payload=None, request_id=""):
            self.calls.append((dict(headers or {}), dict(payload or {}), request_id))
            return RequestContext(request_id=request_id, audience="ops")

    resolver = CountingResolver()
    set_policy_context_resolver(resolver)
    app = SemanticLayerASGIApp(package_id="jaffle_shop")
    try:
        status, payload = asyncio.run(
            _asgi_call(
                app,
                method="POST",
                path="/api/v1/catalog",
                payload={"policy_context": {"audience": "spoofed"}},
            )
        )
        assert status == 200
        assert payload["request_context"]["audience"] == "ops"
        assert len(resolver.calls) == 1
    finally:
        asyncio.run(app.aclose())
        set_policy_context_resolver(HeaderPolicyContextResolver())


@pytest.mark.parametrize("route", ["validate", "compile", "query"])
def test_asgi_partial_resolver_drops_all_outer_and_nested_policy_spoofs(route: str) -> None:
    class TenantOnlyResolver:
        def __init__(self):
            self.calls = []

        def resolve(self, headers, *, payload=None, request_id=""):
            self.calls.append(dict(payload or {}))
            return RequestContext(request_id=request_id, tenant="trusted-tenant")

    resolver = TenantOnlyResolver()
    set_policy_context_resolver(resolver)
    app = SemanticLayerASGIApp(package_id="jaffle_shop")
    request = {
        "policy_context": {
            "tenant": "spoofed-outer-tenant",
            "environment": "spoofed-outer-environment",
        },
        "query": {
            "version": 1,
            "select": [
                {
                    "expression": {"measure": "measure.jaffle.order_count"},
                    "as": "orders",
                }
            ],
            "limit": 1,
            "policy_context": {
                "actor": "spoofed-nested-actor",
                "tenant": "spoofed-nested-tenant",
                "roles": ["debug"],
                "environment": "spoofed-nested-environment",
                "audience": "spoofed-nested-audience",
            },
        },
    }
    try:
        status, payload = asyncio.run(
            _asgi_call(app, method="POST", path=f"/api/v1/{route}", payload=request)
        )

        assert status == 200, payload
        assert payload["request_context"] == {"tenant": "trusted-tenant"}
        assert payload["query"]["policy_context"] == {"tenant": "trusted-tenant"}
        assert "roles" not in payload["request_context"]
        serialized = json.dumps(payload)
        assert "spoofed-" not in serialized
        # The resolver still receives every trusted-local body location. Its
        # output, not this raw input, is the complete runtime authority.
        assert resolver.calls == [
            {
                "policy_context": {
                    "actor": "spoofed-nested-actor",
                    "tenant": "spoofed-outer-tenant",
                    "roles": ["debug"],
                    "environment": "spoofed-outer-environment",
                    "audience": "spoofed-nested-audience",
                }
            }
        ]
    finally:
        asyncio.run(app.aclose())
        set_policy_context_resolver(HeaderPolicyContextResolver())


def test_asgi_get_catalog_query_context_is_resolver_input_not_runtime_authority(
    monkeypatch,
) -> None:
    class TenantOnlyResolver:
        def __init__(self):
            self.calls = []

        def resolve(self, headers, *, payload=None, request_id=""):
            self.calls.append(dict(payload or {}))
            return RequestContext(request_id=request_id, tenant="trusted-tenant")

    captured: list[dict] = []

    def fake_catalog(_runtime, **kwargs):
        captured.append(dict(kwargs.get("policy_context", {}) or {}))
        return {"source": "test"}

    import semantic_rails.http_core as http_core

    monkeypatch.setattr(http_core, "resolve_catalog", fake_catalog)
    resolver = TenantOnlyResolver()
    set_policy_context_resolver(resolver)
    app = SemanticLayerASGIApp(package_id="jaffle_shop")
    try:
        status, payload = asyncio.run(
            _asgi_call(
                app,
                path="/api/v1/catalog",
                query_string="environment=spoofed-environment&audience=spoofed-audience",
            )
        )

        assert status == 200, payload
        assert captured == [{"tenant": "trusted-tenant"}]
        assert resolver.calls == [
            {
                "policy_context": {
                    "environment": "spoofed-environment",
                    "audience": "spoofed-audience",
                }
            }
        ]
        assert payload["request_context"] == {
            "request_id": payload["request_id"],
            "tenant": "trusted-tenant",
        }
    finally:
        asyncio.run(app.aclose())
        set_policy_context_resolver(HeaderPolicyContextResolver())


def test_asgi_bounded_queue_rejects_overload_but_keeps_health_responsive(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original_handle = SemanticHTTPService.handle

    def slow_handle(self, method, route, *args, **kwargs):
        if route == "/catalog":
            entered.set()
            assert release.wait(timeout=5)
        return original_handle(self, method, route, *args, **kwargs)

    monkeypatch.setattr(SemanticHTTPService, "handle", slow_handle)
    app = SemanticLayerASGIApp(
        package_id="jaffle_shop",
        max_workers=1,
        max_in_flight=1,
    )

    async def scenario():
        first = asyncio.create_task(_asgi_call(app, path="/api/v1/catalog"))
        assert await asyncio.to_thread(entered.wait, 2)
        busy = await _asgi_call(app, path="/api/v1/catalog")
        health = await _asgi_call(app, path="/api/v1/health")
        release.set()
        completed = await first
        return busy, health, completed

    busy, health, completed = asyncio.run(scenario())
    asyncio.run(app.aclose())

    assert busy[0] == 503
    assert busy[1]["error"]["code"] == "SERVER_BUSY"
    assert health[0] == 200
    assert health[1]["ok"] is True
    assert completed[0] == 200


def test_asgi_cancelled_request_holds_admission_until_executor_work_finishes(monkeypatch):
    """Client cancellation must not reopen a slot while its thread still runs."""

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_handle = SemanticHTTPService.handle

    def slow_handle(self, method, route, *args, **kwargs):
        if route == "/catalog" and not release.is_set():
            entered.set()
            try:
                assert release.wait(timeout=5)
            finally:
                finished.set()
        return original_handle(self, method, route, *args, **kwargs)

    monkeypatch.setattr(SemanticHTTPService, "handle", slow_handle)
    app = SemanticLayerASGIApp(
        package_id="jaffle_shop",
        max_workers=1,
        max_in_flight=1,
    )

    async def scenario():
        request = asyncio.create_task(_asgi_call(app, path="/api/v1/catalog"))
        assert await asyncio.to_thread(entered.wait, 2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        # The coroutine is gone, but its executor callable is still blocked.
        # Admission must remain closed, while the inline health route remains
        # available, until that callable truly completes.
        busy = await _asgi_call(app, path="/api/v1/catalog")
        health = await _asgi_call(app, path="/api/v1/health")
        assert not finished.is_set()

        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        for _ in range(100):
            if app._work_slots.acquire(blocking=False):
                app._work_slots.release()
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("executor completion did not return the admission slot")

        recovered = await _asgi_call(app, path="/api/v1/catalog")
        return busy, health, recovered

    try:
        busy, health, recovered = asyncio.run(scenario())
    finally:
        release.set()
        asyncio.run(app.aclose())

    assert busy[0] == 503
    assert busy[1]["error"]["code"] == "SERVER_BUSY"
    assert health[0] == 200
    assert health[1]["ok"] is True
    assert recovered[0] == 200


def test_asgi_shutdown_drains_inflight_work_before_closing_runtime(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    runtime_closed = threading.Event()
    original_handle = SemanticHTTPService.handle

    def slow_handle(self, method, route, *args, **kwargs):
        if route == "/catalog":
            entered.set()
            assert release.wait(timeout=5)
        return original_handle(self, method, route, *args, **kwargs)

    monkeypatch.setattr(SemanticHTTPService, "handle", slow_handle)
    app = SemanticLayerASGIApp(package_id="jaffle_shop", max_workers=1, max_in_flight=1)
    original_close = app.runtime.close

    def observed_close():
        runtime_closed.set()
        original_close()

    monkeypatch.setattr(app.runtime, "close", observed_close)

    async def scenario():
        request = asyncio.create_task(_asgi_call(app, path="/api/v1/catalog"))
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(app.aclose())
        await asyncio.sleep(0.05)
        assert not closing.done()
        assert not runtime_closed.is_set()
        release.set()
        response = await request
        await closing
        return response

    response = asyncio.run(scenario())

    assert response[0] == 200
    assert runtime_closed.is_set()


def test_asgi_lifespan_emits_clean_startup_and_shutdown(monkeypatch):
    """ASGI-specific: lifespan startup/shutdown messages must be emitted
    so uvicorn can manage the worker lifecycle. The discover route is
    exercised after lifespan to confirm the app routes HTTP normally.
    """
    monkeypatch.delenv("SEMANTIC_RAILS_API_KEYS", raising=False)
    app = SemanticLayerASGIApp(package_id="jaffle_shop")

    discover_status, discover = asyncio.run(
        _asgi_call(
            app,
            method="POST",
            path="/api/v1/discover",
            payload={"terms": "orders", "limit": 2},
        )
    )
    lifespan_messages = asyncio.run(_asgi_lifespan_call(app))

    assert lifespan_messages == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]
    assert discover_status == 200
    assert discover["measures"] or discover["metrics"] or discover["dimensions"]


def test_asgi_package_load_failure_app_serves_503_after_clean_startup():
    """ASGI-specific: if package load fails, the ASGI app must still
    complete lifespan startup so uvicorn keeps the worker alive and
    routes HTTP requests to the 503 envelope. Sending
    ``lifespan.startup.failed`` would abort the worker and the operator
    would never see the structured error.
    """
    app = PackageLoadFailureApp(
        package_id="missing_pkg",
        error=RuntimeError("config not found"),
    )

    lifespan_messages = asyncio.run(_asgi_lifespan_call(app))
    assert lifespan_messages == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]

    status, body = asyncio.run(_asgi_call(app, path="/api/v1/discover"))
    assert status == 503
    assert body.get("ok") is False
    codes = [err.get("code", "") for err in body.get("errors", [])]
    assert "PACKAGE_LOAD_FAILED" in codes
