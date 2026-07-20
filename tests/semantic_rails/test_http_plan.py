"""HTTP route tests for the public ``plan`` intent surface."""

from __future__ import annotations

from semantic_rails.http_core import SemanticHTTPService, normalize_route


def _post_json(runtime, path: str, payload: dict) -> tuple[dict, int]:
    route = normalize_route(path)
    return SemanticHTTPService(runtime).handle("POST", route, payload)


def _post_json_enveloped(runtime, path: str, payload: dict) -> tuple[dict, int]:
    route = normalize_route(path)
    service = SemanticHTTPService(runtime)
    try:
        return service.handle("POST", route, payload)
    except Exception as exc:  # noqa: BLE001 - mirrors transport wrappers.
        return service.exception_payload(exc, stage="http")


def test_plan_route_returns_status_ok_for_realizable_intent(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload, status = _post_json(
            runtime,
            "/api/v1/plan",
            {"intent": "top stores by revenue"},
        )
    finally:
        runtime.close()
    assert status == 200
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["best"]["pattern"] == "metric_by_dimension_rollup"


def test_plan_route_preserves_partial_query(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload, status = _post_json(
            runtime,
            "/api/v1/plan",
            {
                "intent": "revenue",
                "query": {"version": 1, "group_by": ["dimension.jaffle_store_name"]},
            },
        )
    finally:
        runtime.close()
    assert status == 200
    assert payload["status"] == "ok"
    assert "dimension.jaffle_store_name" in payload["best"]["query_ir"]["group_by"]


def test_plan_route_full_detail_returns_alternatives(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload, status = _post_json(
            runtime,
            "/api/v1/plan",
            {"intent": "top stores by revenue", "detail": "full", "limit": 3},
        )
    finally:
        runtime.close()
    assert status == 200
    assert payload["status"] == "ok"
    assert "alternatives" in payload
    assert "blocked" in payload


def test_old_intent_routes_are_not_public(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        for route in (
            "/api/v1/formulate",
            "/api/v1/propose",
            "/api/v1/parse-intent",
            "/api/v1/expand",
        ):
            payload, status = _post_json(runtime, route, {"intent": "top stores by revenue"})
            assert payload["ok"] is False
            assert status == 404
    finally:
        runtime.close()


def test_plan_route_rejects_missing_empty_or_non_string_intent(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        for body in ({}, {"intent": ""}, {"intent": "   "}, {"intent": 123}):
            payload, status = _post_json_enveloped(runtime, "/api/v1/plan", body)
            assert status == 400
            assert payload["ok"] is False
            assert payload["error"]["code"] == "INVALID_QUERY"
            assert payload["error"]["details"]["path"] == "intent"
    finally:
        runtime.close()
