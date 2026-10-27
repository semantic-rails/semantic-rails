"""HTTP route tests for the public ``plan`` intent surface."""

from __future__ import annotations

import pytest

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


@pytest.fixture()
def runtime(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    yield runtime
    runtime.close()


def test_plan_route_preserves_partial_query(runtime) -> None:
    payload, status = _post_json(
        runtime,
        "/api/v1/plan",
        {"intent": "revenue", "query": {"group_by": ["dimension.jaffle_store_name"]}},
    )
    assert status == 200
    assert payload["status"] == "ok"
    assert "dimension.jaffle_store_name" in payload["best"]["query"]["group_by"]


def test_plan_route_full_detail_returns_alternatives(runtime) -> None:
    payload, status = _post_json(
        runtime, "/api/v1/plan", {"intent": "top stores by revenue", "detail": "full", "limit": 3}
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["best"]["pattern"] == "package_example"
    assert "alternatives" in payload
    assert "blocked" in payload


def test_old_intent_routes_are_not_public(runtime) -> None:
    for route in (
        "/api/v1/formulate",
        "/api/v1/propose",
        "/api/v1/parse-intent",
        "/api/v1/expand",
    ):
        payload, status = _post_json(runtime, route, {"intent": "top stores by revenue"})
        assert payload["ok"] is False
        assert status == 404


def test_plan_route_rejects_missing_empty_or_non_string_intent(runtime) -> None:
    for body in ({}, {"intent": ""}, {"intent": "   "}, {"intent": 123}):
        payload, status = _post_json_enveloped(runtime, "/api/v1/plan", body)
        assert status == 400
        assert payload["ok"] is False
        assert payload["error"]["code"] == "INVALID_QUERY"
        assert payload["error"]["details"]["path"] == "intent"
