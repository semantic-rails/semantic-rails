"""Resource grants are authority across shared runtime and transport surfaces."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest

from semantic_rails.asgi import SemanticLayerASGIApp
from semantic_rails.catalog_service import resolve_catalog
from semantic_rails.db import WarehouseAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import MetricRecipeRefExpr, RatioExpr
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.mcp_server import MCP_PROTOCOL_VERSION
from semantic_rails.metadata import build_options_payload, discover_payload, inspect_payload
from semantic_rails.metadata_parts.valid_values import valid_values_payload
from semantic_rails.planner.plan import plan_payload
from semantic_rails.request_context import (
    RequestContext,
    context_from_policy_context,
    extract_bearer_or_api_key,
    get_policy_context_resolver,
    set_policy_context_resolver,
)
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

CUSTOMERS = "metric.sales.customer_count"
AOV = "metric.sales.aov_usd"
DIMENSION = "dimension.jaffle_customer_type"
SECRET_DIMENSION = "dimension.jaffle_store_name"


class RecordingAdapter(WarehouseAdapter):
    engine = "duckdb"

    def __init__(self):
        self.statements = []

    def query(self, sql, *, limits=None):
        self.statements.append(sql)
        return [{"value": 7}]

    def close(self):
        pass


@pytest.fixture
def granted_runtime(package_config_factory):
    config, path = package_config_factory("jaffle_shop")
    runtime = Runtime.from_config(config, source_path=str(path))
    runtime.set_adapter(RecordingAdapter())
    try:
        yield runtime
    finally:
        runtime.close()


def context(metric=CUSTOMERS, dimensions=(DIMENSION,)):
    return RequestContext(
        actor="subject",
        roles=("analyst",),
        audience="finance",
        metric_allowlist=(metric,) if metric else (),
        dimension_allowlist=dimensions,
    )


def query(metric=CUSTOMERS, ctx=None, **extra):
    return {
        "version": 1,
        "select": [{"expression": {"metric": metric}, "as": "value"}],
        "policy_context": (ctx or context()).to_policy_context(),
        **extra,
    }


@pytest.mark.parametrize(
    "metric,dimensions", [(None, None), ((), ()), ((CUSTOMERS,), (DIMENSION,))]
)
def test_context_roundtrip_preserves_absence_and_empty(metric, dimensions):
    original = RequestContext(metric_allowlist=metric, dimension_allowlist=dimensions)
    assert context_from_policy_context(original.to_policy_context()) == original
    assert "metric_allowlist" not in original.to_public_dict()
    assert "dimension_allowlist" not in original.to_public_dict()
    if metric == ():
        assert original.to_policy_context()["metric_allowlist"] == []


@pytest.mark.parametrize("invalid", ["*", False, {}, [None], [""], [1]])
def test_invalid_grants_fail_closed(invalid):
    with pytest.raises(SemanticLayerError, match="not permitted"):
        context_from_policy_context({"metric_allowlist": invalid})


def test_none_is_legacy_and_empty_grant_denies(granted_runtime):
    runtime = granted_runtime
    legacy = query(ctx=RequestContext())
    assert runtime.compile(legacy)["ok"]
    denied = query(ctx=context(metric=""))
    assert not runtime.validate(denied)["ok"]
    with pytest.raises(SemanticLayerError) as exc:
        runtime.query(denied)
    assert exc.value.code == "RESOURCE_ACCESS_DENIED"
    assert not runtime.adapter.statements
    catalog = resolve_catalog(runtime, policy_context=context(metric="").to_policy_context())
    assert catalog["metrics"] == catalog["dimensions"] == []


@pytest.mark.parametrize("metric,hidden", [(CUSTOMERS, AOV), (AOV, CUSTOMERS)])
def test_disjoint_catalog_discovery_inspect_plan_query(granted_runtime, metric, hidden):
    runtime = granted_runtime
    ctx = context(metric)
    policy = ctx.to_policy_context()
    partial = {"policy_context": policy}
    outputs = [
        resolve_catalog(runtime, policy_context=policy, verbosity="full"),
        resolve_catalog(runtime, policy_context=policy, verbosity="summary"),
        discover_payload(runtime, terms="", partial_query=partial),
        inspect_payload(runtime, object_id=metric, partial_query=partial),
        build_options_payload(runtime, partial_query=partial),
    ]
    for output in outputs:
        serialized = json.dumps(output)
        assert metric in serialized
        assert hidden not in serialized
        assert "measure.jaffle" not in serialized
        assert SECRET_DIMENSION not in serialized
    plan = plan_payload(runtime, intent=metric, partial_query=partial, detail="debug")
    assert plan["best"]["validation_ok"]
    portable = plan["best"]["query_ir"]
    assert "policy_context" not in portable
    assert portable["select"][0]["expression"] == {"metric": metric}
    compiled = runtime.compile({**portable, "policy_context": policy})
    assert compiled["ok"]
    assert compiled["rendered_sql"]
    assert runtime.query({**portable, "policy_context": policy})["rows"] == [{"value": 7}]
    assert len(runtime.adapter.statements) == 1
    forbidden_plan = plan_payload(runtime, intent=hidden, partial_query=partial, detail="debug")
    assert forbidden_plan["best"] is None
    assert hidden not in json.dumps(forbidden_plan)
    with pytest.raises(SemanticLayerError) as exc:
        inspect_payload(runtime, object_id=hidden, partial_query=partial)
    assert hidden not in str(exc.value)


@pytest.mark.parametrize(
    "expression",
    [
        {"measure": "measure.jaffle.revenue_usd"},
        {"kind": "column", "column": "price", "entity": "entity.jaffle_orders"},
        {
            "kind": "aggregate_if",
            "aggregation": "sum",
            "condition": {"kind": "literal", "value": True},
            "value": {"kind": "column", "column": "price", "entity": "entity.jaffle_orders"},
        },
        {"kind": "ratio", "numerator": {"metric": CUSTOMERS}, "denominator": {"metric": AOV}},
        {
            "kind": "arithmetic",
            "op": "add",
            "left": {"metric": CUSTOMERS},
            "right": {"metric": AOV},
        },
        {
            "kind": "distribution",
            "function": "avg",
            "over": {
                "kind": "entity_value",
                "entity": "entity.jaffle_customers",
                "input": {"metric": AOV},
            },
        },
    ],
)
def test_raw_and_nested_expression_bypasses_stop_before_compilation(
    granted_runtime, monkeypatch, expression
):
    runtime = granted_runtime
    monkeypatch.setattr(
        runtime, "_compile", lambda *args, **kwargs: pytest.fail("denied query compiled")
    )
    payload = query(select=[{"expression": expression, "as": "value"}])
    assert not runtime.validate(payload)["ok"]
    with pytest.raises(SemanticLayerError) as exc:
        runtime.query(payload)
    assert exc.value.details == {}
    assert not runtime.adapter.statements


@pytest.mark.parametrize(
    "extra",
    [
        {"group_by": [SECRET_DIMENSION]},
        {"where": [{"field": SECRET_DIMENSION, "op": "=", "value": "test"}]},
        {"select": [], "group_by": [DIMENSION]},
        {"time": {"grain": "month"}},
        {"time": {"temporal_role": "temporal_role.hidden", "grain": "month"}},
        {"temporal_role_overrides": {CUSTOMERS: "temporal_role.hidden"}},
        {"metric_filters": [{"expression": {"metric": AOV}, "op": ">", "value": 1}]},
        {"order_by": [{"field": SECRET_DIMENSION}]},
    ],
)
def test_dimension_and_filter_bypasses_deny_before_execution(granted_runtime, extra):
    assert not granted_runtime.validate(query(**extra))["ok"]
    with pytest.raises(SemanticLayerError):
        granted_runtime.query(query(**extra))
    assert not granted_runtime.adapter.statements


def test_allowed_grouping_and_default_empty_dimensions(granted_runtime):
    assert granted_runtime.query(query(group_by=[DIMENSION]))["ok"]
    assert len(granted_runtime.adapter.statements) == 1
    no_dimensions = context(dimensions=None)
    assert not granted_runtime.validate(query(ctx=no_dimensions, group_by=[DIMENSION]))["ok"]
    assert len(granted_runtime.adapter.statements) == 1


def test_explicit_time_grant_and_no_unscoped_coverage_probe(granted_runtime, monkeypatch):
    role = "temporal_role.jaffle_customer_first_order_at"
    payload = query(ctx=context(dimensions=(role,)), time={"temporal_role": role, "grain": "month"})
    assert granted_runtime.compile(payload)["ok"]
    adapter = granted_runtime.adapter

    def no_rows(sql, *, limits=None):
        adapter.statements.append(sql)
        return []

    monkeypatch.setattr(adapter, "query", no_rows)
    assert granted_runtime.query(payload)["rows"] == []
    assert len(adapter.statements) == 1
    assert not granted_runtime.validate(query(time={"temporal_role": role, "grain": "month"}))["ok"]


def test_restricted_plan_does_not_silently_drop_unsupported_intent(granted_runtime):
    partial = {"policy_context": context().to_policy_context()}
    for intent in [
        f"{CUSTOMERS} by secret region",
        f"{CUSTOMERS} in 2025",
        f"{CUSTOMERS} and {AOV}",
    ]:
        result = plan_payload(granted_runtime, intent=intent, partial_query=partial)
        assert result["best"] is None
        assert "secret region" not in json.dumps(result)
        assert AOV not in json.dumps(result)
    assert not granted_runtime.adapter.statements


def test_grants_and_revocation_are_checked_before_cached_compilation(granted_runtime):
    runtime = granted_runtime
    allowed = query(verbosity="full")
    assert not runtime.compile(allowed)["compile_stats"]["cache_hit"]
    assert runtime.compile(allowed)["compile_stats"]["cache_hit"]
    with pytest.raises(SemanticLayerError):
        runtime.compile(query(ctx=context(AOV), verbosity="full"))
    runtime.config.semantic_policies.append(
        SemanticPolicyConfig(
            id="policy.private",
            kind="object_access",
            object_ids=[CUSTOMERS],
            roles=["analyst"],
            audiences=["finance"],
            action="deny",
        )
    )
    with pytest.raises(SemanticLayerError) as exc:
        runtime.compile(allowed)
    assert "private" not in str(exc.value)
    assert resolve_catalog(runtime, policy_context=context().to_policy_context())["metrics"] == []


def test_parallel_catalogs_do_not_mutate_runtime(granted_runtime):
    runtime = granted_runtime
    config, registry = runtime.config, runtime.registry
    metrics = [CUSTOMERS, AOV] * 8

    def call(metric):
        return resolve_catalog(runtime, policy_context=context(metric).to_policy_context())[
            "metrics"
        ]

    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(call, metrics))
    assert [[row["id"] for row in output] for output in outputs] == [[metric] for metric in metrics]
    assert runtime.config is config
    assert runtime.registry is registry


def test_package_policy_follows_nested_internal_recipe_dependencies(granted_runtime, monkeypatch):
    runtime = granted_runtime
    runtime.config.metric_recipes[:] = [
        replace(row, expression=RatioExpr(MetricRecipeRefExpr(AOV), MetricRecipeRefExpr(AOV)))
        if row.id == CUSTOMERS
        else row
        for row in runtime.config.metric_recipes
    ]
    runtime.config.semantic_policies.append(
        SemanticPolicyConfig(
            id="policy.hidden_recipe",
            kind="object_access",
            object_ids=[AOV],
            action="deny",
        )
    )
    monkeypatch.setattr(
        runtime, "_compile", lambda *args, **kwargs: pytest.fail("denied dependency compiled")
    )
    assert not runtime.validate(query())["ok"]
    catalog = resolve_catalog(runtime, policy_context=context().to_policy_context())
    assert not catalog["metrics"]
    assert AOV not in json.dumps(catalog)
    assert not runtime.adapter.statements


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_authenticated_asgi_grants_roundtrip_and_revoke(
    package_config_factory, monkeypatch, transport
):
    class Resolver:
        def resolve(self, headers, *, payload=None, request_id=""):
            token = extract_bearer_or_api_key(headers)
            return replace(
                context(CUSTOMERS if token == "customers" else AOV), request_id=request_id
            )

    previous = get_policy_context_resolver()
    set_policy_context_resolver(Resolver())
    monkeypatch.setenv("SEMANTIC_RAILS_API_KEYS", "customers,aov")
    _, path = package_config_factory("jaffle_shop")
    app = SemanticLayerASGIApp(path=str(path), max_workers=2)
    adapter = RecordingAdapter()
    app.runtime.set_adapter(adapter)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:

            async def call(operation, arguments, token="customers"):
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                    "X-Semantic-Roles": "admin",
                    "X-Semantic-Audience": "spoof",
                }
                body = arguments
                route = f"/api/v1/{operation}"
                if transport == "mcp":
                    route = "/mcp"
                    body = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "execute" if operation == "query" else operation,
                            "arguments": arguments,
                        },
                    }
                response = await client.post(route, json=body, headers=headers)
                result = response.json()
                return result["result"]["structuredContent"] if transport == "mcp" else result

            spoof = context(AOV, (SECRET_DIMENSION,)).to_policy_context()
            plan = await call(
                "plan",
                {"intent": CUSTOMERS, "policy_context": spoof, "query": {"policy_context": spoof}},
            )
            assert plan["best"]["validation_ok"]
            portable = plan["best"]["query_ir"]
            assert "policy_context" not in portable
            assert plan["request_context"]["roles"] == ["analyst"]
            assert plan["request_context"]["audience"] == "finance"
            assert (await call("query", {"query": portable}))["rows"] == [{"value": 7}]
            assert len(adapter.statements) == 1
            denied = await call(
                "query",
                {"query": {**portable, "policy_context": spoof}, "policy_context": spoof},
                "aov",
            )
            assert not denied["ok"]
            assert len(adapter.statements) == 1
            catalogs = await asyncio.gather(
                call("catalog", {"policy_context": spoof}), call("catalog", {}, "aov")
            )
            assert AOV not in json.dumps(catalogs[0])
            assert CUSTOMERS not in json.dumps(catalogs[1])

    try:
        asyncio.run(run())
    finally:
        asyncio.run(app.aclose())
        set_policy_context_resolver(previous)


def test_manifest_and_unsupported_operations_cannot_bypass(granted_runtime, monkeypatch):
    runtime = granted_runtime
    monkeypatch.setattr(
        runtime, "manifest_catalog", lambda **kwargs: pytest.fail("anonymous manifest used")
    )
    resolve_catalog(runtime, policy_context=context(metric="").to_policy_context())
    for call in [
        lambda: valid_values_payload(
            runtime, dimension_id=DIMENSION, query=query(), allow_live_query=True
        ),
        lambda: runtime.segment_explain(
            "segment.hidden", policy_context=context().to_policy_context()
        ),
        lambda: runtime.segment_preview(
            "segment.hidden", policy_context=context().to_policy_context()
        ),
    ]:
        with pytest.raises(SemanticLayerError):
            call()
    assert not runtime.adapter.statements


@pytest.mark.parametrize("transport", ["http", "mcp"])
@pytest.mark.parametrize(
    "operation", ["catalog", "discover", "inspect", "plan", "compile", "execute"]
)
def test_transport_trusted_context_overrides_nested_grant_forgery(
    granted_runtime, transport, operation
):
    runtime = granted_runtime
    trusted = context()
    spoof = {
        "roles": ["admin"],
        "audience": "public",
        "metric_allowlist": [AOV],
        "dimension_allowlist": [SECRET_DIMENSION],
    }
    arguments = {"policy_context": spoof}
    if operation in {"compile", "execute"}:
        arguments["query"] = query(AOV, ctx=context(AOV))
        arguments["query"]["policy_context"] = spoof
    elif operation == "inspect":
        arguments["object_id"] = AOV
    elif operation == "plan":
        arguments["intent"] = AOV
        arguments["query"] = {"policy_context": spoof}
    elif operation == "discover":
        arguments["terms"] = ""
    if transport == "http":
        service = SemanticHTTPService(runtime)
        try:
            result, _ = service.handle(
                "POST",
                "/query" if operation == "execute" else f"/{operation}",
                arguments,
                context=trusted,
            )
        except SemanticLayerError as exc:
            result = {"ok": False, "error": {"code": exc.code}}
    else:
        result = SemanticLayerMCPAdapter(runtime).call_tool(
            operation, arguments, request_context=trusted
        )
    if operation in {"inspect", "compile", "execute"}:
        assert not result["ok"]
    else:
        assert AOV not in json.dumps(result)
        assert SECRET_DIMENSION not in json.dumps(result)
    assert not runtime.adapter.statements
