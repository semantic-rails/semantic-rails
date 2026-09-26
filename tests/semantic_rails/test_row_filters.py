"""Row-filter policies: each customer's rows only, on every surface, or a denial."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace

import duckdb
import httpx
import pytest
import yaml

from semantic_rails import runtime as runtime_module
from semantic_rails.asgi import SemanticLayerASGIApp
from semantic_rails.audit import get_audit_sink, set_audit_sink
from semantic_rails.config import load_package_config
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp_server import MCP_PROTOCOL_VERSION
from semantic_rails.policies import row_filters_for_context
from semantic_rails.renderer import render_select
from semantic_rails.request_context import get_policy_context_resolver, set_policy_context_resolver
from semantic_rails.row_filters import RowFilter, apply_row_filters
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from semantic_rails.sql_ast import (
    SqlBinary,
    SqlField,
    SqlIdentifier,
    SqlLiteral,
    SqlSelect,
    SqlTableFunction,
    SqlTableRef,
)
from semantic_rails.sql_preparation import ParameterSlot

A, B = "cust-a-canary-4417", "cust-b-canary-9023"
SEED = f"""
CREATE TABLE order_fact AS SELECT * FROM (VALUES
 (1, '{A}', 's1', TIMESTAMP '2026-01-10', 10), (2, '{A}', 's2', TIMESTAMP '2026-02-10', 20),
 (3, '{B}', 's3', TIMESTAMP '2026-01-20', 300), (4, '{B}', 's1', TIMESTAMP '2026-02-20', 400)
) t(order_id, customer_id, store_id, ordered_at, amount);
CREATE TABLE store_dim AS SELECT * FROM (VALUES ('s1', 'North'), ('s2', 'East'), ('s3', 'West'))
 t(store_id, store_name);
CREATE TABLE calendar_day AS SELECT d::DATE AS date_day, date_trunc('month', d)::DATE AS month_start
 FROM range(DATE '2026-01-01', DATE '2026-03-01', INTERVAL 1 DAY) t(d);
CREATE TABLE order_monthly AS SELECT date_trunc('month', ordered_at) AS month_start, store_id,
 sum(amount) AS revenue FROM order_fact GROUP BY 1, 2;
"""
REVENUE = {"kind": "aggregate", "measure": "measure.rf.revenue", "aggregation": "sum"}
MONTH = {"temporal_role": "temporal_role.rf_order_ordered_at", "grain": "month"}
BIG_STORE = {
    "expression": {"kind": "metric_predicate", "entity": "entity.rf_store", "input": REVENUE,
                   "op": ">=", "value": 100},
    "op": "=",
    "value": True,
}  # fmt: skip
BY_STORE = {
    "version": 1,
    "select": [
        {"expression": REVENUE, "as": "revenue"},
        {"expression": {"metric": "metric.rf.revenue_per_order"}, "as": "per_order"},
    ],
    "group_by": ["dimension.rf_order_store_id"],
    "order_by": [{"field": "dimension.rf_order_store_id", "direction": "ASC"}],
}
OWN_ORDERS = {
    "id": "policy.rf.own_orders",
    "kind": "row_filter",
    "dimension": "dimension.rf_order_customer_id",
    "attribute": "customer_id",
    "audiences": ["customer"],
}
# Declared categorical while the column is INTEGER: the driver can't convert a bound string.
MISTYPED = {**OWN_ORDERS, "id": "policy.rf.mistyped", "audiences": ["mistyped"]}
MISTYPED["dimension"] = "dimension.rf_order_order_ref"


def _package(root, policies):
    def put(name, doc):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    time = {"column": "ordered_at", "kind": "timestamp", "class": "event_time", "default": True}
    monthly = {
        "relation": "order_monthly",
        "grain": {"time": "month", "entities": []},
        "time": {"role": "ordered_at", "column": "month_start"},
        "excludes": {"entities": ["order"], "dimensions": ["customer_id"]},
        "columns": {"store_id": "store_id", "revenue": "revenue"},
        "eligible_time_grains": ["month"],
        "equivalence": {"kind": "exact"},
    }
    tx = {"relation": "order_fact", "grain": {"time": "transaction", "entities": ["order"]}}
    put("package.yml", {
        "schema_version": 1,
        "package": {"id": "rf", "name": "rf", "description": "Row filters", "warehouse": "duckdb",
                    "default_db": "rf.duckdb", "seed": {"kind": "external"}},
        "defaults": {"time": {"timezone": "UTC", "default_query_axis": False}},
    })  # fmt: skip
    put("graph.yml", {"graph": {
        "entities": {
            "order": {"key": ["order_id"], "model": "orders"},
            "store": {"key": ["store_id"], "model": "stores"},
            "time": {"kind": "time", "key": ["date_day"], "model": "calendar",
                     "allowed_as_root": False},
        },
        "relationships": {"orders_store": {"id": "relationship.orders_store",
                                           "entities": ["order", "store"],
                                           "cardinality": "many_to_one"}},
    }})  # fmt: skip
    put("models/orders.yml", {"model": {
        "id": "orders", "relation": "order_fact", "entities": {"order": {}, "store": {}},
        "times": {"ordered_at": time},
        "dimensions": {"customer_id": {"kind": "categorical"},
                       "order_ref": {"column": "order_id", "kind": "categorical"}},
        "measures": {
            "revenue": {"kind": "aggregate", "expr": "amount", "rollup": "additive"},
            "order_count": {"kind": "entity_count", "entity_key": "order_id", "rollup": "additive"},
        },
        "default_variant": "tx", "variants": {"tx": {**tx, "covers": "inherit_all"}, "monthly": monthly},
    }})  # fmt: skip
    put("models/stores.yml", {"model": {"id": "stores", "relation": "store_dim", "entities": {"store": {}},
                                        "dimensions": {"store_name": {"kind": "categorical"}}}})  # fmt: skip
    put("models/calendar.yml", {"model": {
        "id": "calendar", "relation": "calendar_day", "calendar_id": "default", "entities": {"time": {}},
        "times": {"date_day": {"column": "date_day", "kind": "date", "class": "calendar_time"}},
        "dimensions": {"month_start": {"kind": "date"}},
    }})  # fmt: skip
    prior = {"kind": "prior_period", "input": REVENUE, "offset": {"unit": "month", "value": 1}}
    put("metrics/core.yml", {"metrics": {
        "rf.revenue_per_order": {"as": "metric.rf.revenue_per_order", "kind": "ratio",
                                 "numerator": "revenue", "denominator": "order_count"},
        "rf.revenue_change": {"as": "metric.rf.revenue_change", "kind": "derived",
                              "temporal_role": "temporal_role.rf_order_ordered_at",
                              "expression": {"kind": "binary", "op": "-", "left": REVENUE,
                                             "right": prior}},
    }})  # fmt: skip
    in_s1 = [{"field": "dimension.rf_order_store_id", "op": "=", "value": "s1"}]
    put("segments/core.yml", {"segments": {
        "order.in_s1": {"id": "segment.rf.in_s1", "entity": "order",
                        "basis_metric": "metric.rf.revenue", "membership": {"where": in_s1}},
        "order.big_store": {"id": "segment.rf.big_store", "entity": "order",
                            "basis_metric": "metric.rf.revenue",
                            "membership": {"metric_filters": [BIG_STORE]}},
    }})  # fmt: skip
    put("policies.yml", {"semantic_policies": policies})
    return root


@pytest.fixture
def package(tmp_path):
    root = _package(tmp_path / "rf", [OWN_ORDERS, MISTYPED])
    with duckdb.connect(str(root / "rf.duckdb")) as connection:
        connection.execute(SEED)
    return root


@pytest.fixture
def runtime(package):
    return Runtime.from_path(str(package))


def _ctx(audience="customer", **attributes):
    return RequestContext(actor="end-user", audience=audience, attributes=attributes)


def _q(query=BY_STORE, audience="customer", **attributes):
    return {**query, "policy_context": _ctx(audience, **attributes).to_policy_context()}


def _denied(call):
    with pytest.raises(SemanticLayerError) as caught:
        call()
    assert caught.value.code == "POLICY_DENIED"
    return caught.value.details["reason"]


def test_two_customers_get_their_own_aggregates_from_one_statement(runtime):
    a = runtime.query(_q(customer_id=A))
    b = runtime.query(_q(customer_id=B))
    everyone = runtime.query(_q(audience="internal"))

    def rows(result):
        return [
            (r["dimension.rf_order_store_id"], r["revenue"], r["per_order"]) for r in result["rows"]
        ]

    assert rows(a) == [("s1", 10, 10.0), ("s2", 20, 20.0)]
    assert rows(b) == [("s1", 400, 400.0), ("s3", 300, 300.0)]
    assert rows(everyone) == [("s1", 410, 205.0), ("s2", 20, 20.0), ("s3", 300, 300.0)]
    assert a["rendered_sql"] == b["rendered_sql"] != everyone["rendered_sql"]
    assert "order_fact.customer_id = ?" in a["rendered_sql"]
    assert A not in json.dumps(b, default=str) and B not in json.dumps(a, default=str)


@pytest.mark.parametrize("attributes", [{}, {"customer_id": 7}, {"customer_id": [A]}, {"tier": A}])
def test_a_missing_or_mistyped_attribute_denies_every_surface(runtime, attributes):
    query = _q(**attributes)
    context = query["policy_context"]
    reasons = {
        _denied(lambda: runtime.compile(query)),
        _denied(lambda: runtime.query(query)),
        _denied(lambda: runtime.segment_preview("segment.rf.in_s1", policy_context=context)),
        runtime.validate(query)["errors"][0]["details"]["reason"],
    }
    assert reasons <= {"missing_attribute", "attribute_type_mismatch"} and len(reasons) == 1


@pytest.mark.parametrize(
    ("where", "stores"),
    [
        ([{"field": "dimension.rf_order_customer_id", "op": "=", "value": B}], []),
        ([{"field": "dimension.rf_order_customer_id", "op": "IN", "value": [A, B]}], ["s1", "s2"]),
        ([{"field": "dimension.rf_order_store_id", "op": "!=", "value": "s2"}], ["s1"]),
    ],
)
def test_a_caller_predicate_only_narrows(runtime, where, stores):
    result = runtime.query(_q({**BY_STORE, "where": where}, customer_id=A))
    assert [row["dimension.rf_order_store_id"] for row in result["rows"]] == stores


def test_the_filter_stays_outside_an_or_in_existing_conditions():
    either = SqlBinary(SqlLiteral(True), "OR", SqlLiteral(True))
    scan = SqlSelect([SqlField(SqlIdentifier(["t", "v"]), "v")], SqlTableRef("t"), where=[either])
    slot = ParameterSlot("customer_id", "string")
    filtered, slots = apply_row_filters(scan, [RowFilter("p", "t", "c", slot)])
    assert slots == (slot,)
    assert render_select(filtered).endswith("WHERE\n  t.c = ? AND (TRUE OR TRUE)")


@pytest.mark.parametrize(
    "query",
    [
        {**BY_STORE, "group_by": ["dimension.rf_store_store_name"], "order_by": []},  # join
        {**BY_STORE, "metric_filters": [BIG_STORE]},  # a second scan of the filtered relation
        {  # nested dependency: the prior period comes from another scan over a calendar spine
            "version": 1,
            "select": [{"expression": {"metric": "metric.rf.revenue_change"}, "as": "change"}],
            "time": MONTH,
        },
        {  # spine: calendar fill joins the calendar relation
            "version": 1,
            "select": [{"expression": REVENUE, "as": "revenue"}],
            "time": {
                **MONTH,
                "grain": "day",
                "start": "2026-01-01",
                "end": "2026-01-05",
                "fill": True,
            },
        },
    ],
    ids=["join", "metric_filter", "dependency", "spine"],
)
def test_unqualified_shapes_are_denied_not_answered_unfiltered(runtime, query):
    internal = runtime.compile(_q(query, audience="internal"))
    assert internal["ok"] is True
    assert _denied(lambda: runtime.compile(_q(query, customer_id=A))) == (
        "row_filter_unsupported_query"
    )


def test_segment_preview_and_count_bind_the_filter(runtime):
    context = _ctx(customer_id=A).to_policy_context()
    a = runtime.segment_preview("segment.rf.in_s1", policy_context=context)
    b = runtime.segment_preview(
        "segment.rf.in_s1", policy_context=_ctx(customer_id=B).to_policy_context()
    )
    assert (a["member_count"], b["member_count"]) == (1, 1)
    assert a["rows"] != b["rows"] and "?" in a["count_sql"]
    assert (
        _denied(lambda: runtime.segment_preview("segment.rf.big_store", policy_context=context))
        == "row_filter_unsupported_query"
    )


def test_rollups_are_not_routed_under_a_row_filter(runtime):
    query = {"version": 1, "select": [{"expression": REVENUE, "as": "revenue"}], "time": MONTH}
    internal = runtime.compile(_q(query, audience="internal"))
    filtered = runtime.query(_q(query, customer_id=B))
    assert "FROM order_monthly" in internal["rendered_sql"]
    assert (
        "FROM order_fact" in filtered["rendered_sql"]
        and "order_monthly" not in filtered["rendered_sql"]
    )
    assert sorted(row["revenue"] for row in filtered["rows"]) == [300, 400]


def test_zero_rows_skip_the_whole_relation_coverage_probe(runtime, monkeypatch):
    probes = []
    monkeypatch.setattr(
        runtime_module, "_data_coverage_probe", lambda *a, **k: probes.append(1) or {}
    )
    window = {"version": 1, "select": [{"expression": REVENUE, "as": "revenue"}],
              "time": {**MONTH, "start": "2025-01-01", "end": "2025-02-01"}}  # fmt: skip
    filtered = runtime.query(_q(window, customer_id=A))
    assert filtered["row_count"] == 0 and probes == []
    assert filtered["data_diagnostics"]["actual_data_coverage"] == {}
    runtime.query(_q(window, audience="internal"))
    assert probes == [1]


def test_driver_errors_never_echo_a_bound_value(runtime, caplog):
    events = []

    class Sink:
        def emit(self, payload):
            events.append(payload)

    previous = get_audit_sink()
    set_audit_sink(Sink())
    try:
        with (
            caplog.at_level(logging.DEBUG, logger="semantic_rails"),
            pytest.raises(SemanticLayerError) as caught,
        ):
            runtime.query(_q(audience="mistyped", customer_id=A))
    finally:
        set_audit_sink(previous)
    error, chain = caught.value, []
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert chain[0].code == "QUERY_EXECUTION_ERROR"
    assert "ConversionException" in caplog.text
    assert A not in json.dumps([[str(e), getattr(e, "details", {})] for e in chain], default=str)
    assert A not in caplog.text + json.dumps(events, default=str)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"dimension": "dimension.rf_order_ordered_at"}, "has type 'timestamp'"),
        ({"dimension": "dimension.rf_order_store_id"}, "an id dimension needs 'type'"),
        ({"dimension": "dimension.nope"}, "must name a dimension"),
        ({"attribute": "Customer-ID"}, "attribute names must match"),
        ({"op": "!="}, "unsupported keys ['op']"),
        ({"object_ids": ["measure.rf.revenue"]}, "unsupported keys ['object_ids']"),
        ({"type": "integer"}, "has type 'string'"),
    ],
)
def test_the_package_rejects_a_row_filter_it_cannot_enforce(tmp_path, change, problem):
    with pytest.raises(SemanticLayerError, match=re.escape(problem)):
        load_package_config(str(_package(tmp_path / "rf", [{**OWN_ORDERS, **change}])))


def test_an_id_dimension_takes_an_explicit_type(tmp_path):
    policy = {**OWN_ORDERS, "dimension": "dimension.rf_order_store_id", "type": "string"}
    load_package_config(str(_package(tmp_path / "rf", [policy])))


def test_a_row_filter_that_skipped_the_loader_still_fails_closed(runtime):
    unscoped = {"dimension": OWN_ORDERS["dimension"], "attribute": "customer_id"}
    policy = SemanticPolicyConfig("p", "row_filter", unscoped, object_ids=["measure.rf.revenue"])
    config = replace(runtime._config, semantic_policies=[policy])
    with pytest.raises(SemanticLayerError, match="unsupported keys"):
        row_filters_for_context(config, _ctx(audience="internal").to_policy_context())


def test_a_table_function_read_is_not_filterable():
    scan = SqlSelect([SqlField(SqlIdentifier(["v"]), "v")], SqlTableFunction("UNNEST", alias="t"))
    row = RowFilter("p", "t", "c", ParameterSlot("customer_id", "string"))
    with pytest.raises(SemanticLayerError, match="only a query that reads"):
        apply_row_filters(scan, [row])


def test_every_mcp_surface_shows_each_customer_only_their_rows(package, monkeypatch):
    """Plan, execute, segment preview and live valid-values through the MCP transport."""
    customer = {}

    class HostResolver:
        def resolve(self, headers, *, payload=None, request_id=""):
            return _ctx(customer_id=customer["id"])

    events = []

    class Sink:
        def emit(self, payload):
            events.append(payload)

    monkeypatch.setenv("SEMANTIC_RAILS_API_KEYS", "test-row-filter-key")
    monkeypatch.setenv("SEMANTIC_RAILS_AUDIT_LOGS", "1")
    previous = get_policy_context_resolver(), get_audit_sink()
    set_policy_context_resolver(HostResolver())
    set_audit_sink(Sink())
    app = SemanticLayerASGIApp(path=str(package), max_workers=1)

    async def call(tool, arguments):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments}}  # fmt: skip
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer test-row-filter-key",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post("/mcp", json=body, headers=headers)
        return response.json()["result"]["structuredContent"]

    def surfaces(customer_id):
        customer["id"] = customer_id
        out = {"plan": asyncio.run(call("plan", {"intent": "revenue by store"}))}
        query = {key: value for key, value in BY_STORE.items() if key != "order_by"}
        for mode in ("validate", "sql", "run"):
            out[mode] = asyncio.run(call("execute", {"query": query, "mode": mode}))
        preview = {"action": "preview", "segment_id": "segment.rf.in_s1"}
        out["preview"] = asyncio.run(call("segment", preview))
        values = {"dimension_id": "dimension.rf_order_order_ref", "allow_live_query": True}
        out["values"] = asyncio.run(call("valid-values", values))
        return out

    try:
        a, b = surfaces(A), surfaces(B)
    finally:
        asyncio.run(app.aclose())
        set_policy_context_resolver(previous[0])
        set_audit_sink(previous[1])
    assert all(response["ok"] is True for response in [*a.values(), *b.values()]), (a, b)
    assert a["run"]["rows"] != b["run"]["rows"] and a["preview"]["rows"] != b["preview"]["rows"]
    assert [row["value"] for row in a["values"]["values"]] == [1, 2]
    assert B not in json.dumps(a, default=str) and A not in json.dumps(b, default=str)
    assert A not in json.dumps(events, default=str) and B not in json.dumps(events, default=str)
