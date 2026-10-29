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

from semantic_rails import row_filters as row_filters_module
from semantic_rails import runtime as runtime_module
from semantic_rails import sql_preparation
from semantic_rails.asgi import SemanticLayerASGIApp
from semantic_rails.audit import get_audit_sink, set_audit_sink
from semantic_rails.compiler_parts.empty_groups import LeafScope, guard_empty_groups
from semantic_rails.config import load_package_config
from semantic_rails.config_validation import parse_config_report, resolve_package_reference
from semantic_rails.dialects import dialect_for_warehouse
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp_server import MCP_PROTOCOL_VERSION
from semantic_rails.policies import row_filters_for_context
from semantic_rails.renderer import render_select, render_with_slots
from semantic_rails.request_context import (
    HeaderPolicyContextResolver,
    TrustedAttributes,
    context_from_headers,
    get_policy_context_resolver,
    set_policy_context_resolver,
)
from semantic_rails.row_filter_visibility import Visibility
from semantic_rails.row_filters import RowFilter, apply_row_filters
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from semantic_rails.sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCte,
    SqlExists,
    SqlField,
    SqlIdentifier,
    SqlJoin,
    SqlLiteral,
    SqlParameter,
    SqlSelect,
    SqlSetQuery,
    SqlTableFunction,
    SqlTableRef,
    sql_nodes,
)
from semantic_rails.sql_preparation import ParameterSlot

A, B = "cust-a-canary-4417", "cust-b-canary-9023"
# Two customers by UUID, for a filter of type uuid.
UA, UB = "6f1c0b9e-3a52-4c1d-9e7a-2b8d4f0a1c35", "0d9e8f7a-6b5c-4d3e-8f2a-1b0c9d8e7f6a"
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
    "select": [
        {"expression": REVENUE, "as": "revenue"},
        {"expression": "metric.rf.revenue_per_order", "as": "per_order"},
    ],
    "group_by": ["dimension.rf_order_store_id"],
    "order_by": [{"field": "dimension.rf_order_store_id", "direction": "asc"}],
}
# Stores with at least 100 in revenue: a second scan of the filtered relation.
BIG_STORES = {
    "entity": "entity.rf_store",
    "having": [{"field": "measure.rf.revenue", "op": "gte", "value": 100}],
    "over": "all_time",
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
# A second filter on the same relation for callers with this role; id dimensions declare a type.
OWN_STORE = {**OWN_ORDERS, "id": "policy.rf.own_store", "attribute": "store_id", "type": "string"}
OWN_STORE.update(dimension="dimension.rf_order_store_id", audiences=[], roles=["store_scoped"])
OWN_STORE["unless_roles"] = ["store_auditor"]  # exempt only when the host verified the role
DROP = object()  # a key a test case removes


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
        "defaults": {"time": {"timezone": "UTC"}},
    })  # fmt: skip
    put("graph.yml", {"graph": {
        "entities": {
            "order": {"key": ["order_id"], "model": "orders"},
            "store": {"key": ["store_id"], "model": "stores"},
            "time": {"kind": "time", "key": ["date_day"], "model": "calendar",
                     "allowed_as_root": False},
        },
        "relationships": {"orders_store": {"as": "relationship.orders_store",
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
            "s2_count": {"kind": "entity_count", "accumulation": {"kind": "event"}, "expr": {
                "kind": "case", "whens": [{"when": {"kind": "comparison", "op": "=",
                    "left": {"kind": "column", "column": "store_id"}, "right": {"kind": "literal", "value": "s2"}},
                    "then": {"kind": "column", "column": "order_id"}}], "else": {"kind": "literal", "value": None}}},
        },
        "variants": {"tx": tx, "monthly": monthly},
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
        "rf.revenue": {"as": "metric.rf.revenue", "name": "rf.revenue", "kind": "aggregate",
                       "label": "Revenue", "measure": "measure.rf.revenue", "aggregation": "sum",
                       "temporal_role": "temporal_role.rf_order_ordered_at", "value_type": "number"},
        "rf.revenue_per_order": {"as": "metric.rf.revenue_per_order", "kind": "ratio",
                                 "description": "Revenue per order.",
                                 "numerator": "measure.rf.revenue", "denominator": "order_count",
                                 "value_type": "number"},
        "rf.revenue_change": {"as": "metric.rf.revenue_change", "kind": "derived",
                              "description": "Revenue change from the prior month.",
                              "temporal_role": "temporal_role.rf_order_ordered_at",
                              "value_type": "number",
                              "expression": {"kind": "arithmetic", "op": "-", "left": REVENUE,
                                             "right": prior}},
    }})  # fmt: skip
    in_s1 = [{"field": "dimension.rf_order_store_id", "op": "=", "value": "s1"}]
    put("segments/core.yml", {"segments": {
        "order.in_s1": {"as": "segment.rf.in_s1", "entity": "order",
                        "basis_metric": "metric.rf.revenue", "membership": {"where": in_s1}},
        "order.big_store": {"as": "segment.rf.big_store", "entity": "order",
                            "basis_metric": "metric.rf.revenue",
                            "membership": {"metric_filters": [BIG_STORE]}},
    }})  # fmt: skip
    put("policies.yml", {"semantic_policies": policies})
    return root


@pytest.fixture
def package(tmp_path):
    root = _package(tmp_path / "rf", [OWN_ORDERS, MISTYPED, OWN_STORE])
    with duckdb.connect(str(root / "rf.duckdb")) as connection:
        connection.execute(SEED)
    return root


@pytest.fixture
def runtime(package):
    return Runtime.from_path(str(package))


def _ctx(audience="customer", **attributes):
    return RequestContext(actor="end-user", audience=audience, attributes=attributes)


def _as(method, query=BY_STORE, audience="customer", now=None, **attributes):
    """``method(query)`` asked by a caller of ``audience`` with ``attributes``."""
    return method(query, context=_ctx(audience, **attributes), now=now)


def _denied(call):
    with pytest.raises(SemanticLayerError) as caught:
        call()
    assert caught.value.code == "POLICY_DENIED"
    return caught.value.details["reason"]


def _forbidden(*args, **kwargs):
    raise AssertionError("the request must not reach the warehouse")


def test_two_customers_get_their_own_aggregates_from_one_statement(runtime):
    a = _as(runtime.query, customer_id=A)
    b = _as(runtime.query, customer_id=B)
    everyone = _as(runtime.query, audience="internal")

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


@pytest.mark.parametrize(
    "attributes",
    [{}, {"customer_id": 7}, {"customer_id": [7, 8]}, {"customer_id": True}, {"tier": A}],
)
def test_a_missing_or_mistyped_attribute_denies_every_surface(runtime, attributes):
    context = _ctx(**attributes)
    preview = "segment.rf.in_s1"
    reasons = {
        _denied(lambda: runtime.compile(BY_STORE, context=context)),
        _denied(lambda: runtime.query(BY_STORE, context=context)),
        _denied(
            lambda: runtime.segment_preview(preview, policy_context=context.to_policy_context())
        ),
        runtime.validate(BY_STORE, context=context)["errors"][0]["details"]["reason"],
    }
    assert reasons <= {"missing_attribute", "attribute_type_mismatch"} and len(reasons) == 1


@pytest.mark.parametrize(
    ("where", "stores"),
    [
        ([{"field": "dimension.rf_order_customer_id", "op": "is", "value": B}], []),
        ([{"field": "dimension.rf_order_customer_id", "op": "is", "value": [A, B]}], ["s1", "s2"]),
        ([{"field": "dimension.rf_order_store_id", "op": "not", "value": "s2"}], ["s1"]),
    ],
)
def test_a_caller_predicate_only_narrows(runtime, where, stores):
    result = _as(runtime.query, {**BY_STORE, "where": where}, customer_id=A)
    assert [row["dimension.rf_order_store_id"] for row in result["rows"]] == stores


def test_the_filter_stays_outside_an_or_in_existing_conditions():
    either = SqlBinary(SqlLiteral(True), "OR", SqlLiteral(True))
    scan = SqlSelect([SqlField(SqlIdentifier(["t", "v"]), "v")], SqlTableRef("t"), where=[either])
    slot = ParameterSlot("customer_id", "string")
    filtered, slots = _apply(scan, RowFilter("p", "t", "c", slot))
    assert slots == (slot,)
    assert render_select(filtered).endswith("WHERE\n  t.c = ?\n  AND (TRUE OR TRUE)")


def test_a_lookup_and_a_metric_filter_read_only_the_callers_rows(runtime):
    """Stores are reference data no filter reaches: orders may look them up, and a predicate
    on stores reads only the caller's orders."""
    by_name = {**BY_STORE, "group_by": ["dimension.rf_store_store_name"], "order_by": []}
    rows = _as(runtime.query, by_name, customer_id=A)["rows"]
    assert sorted((r["dimension.rf_store_store_name"], r["revenue"]) for r in rows) == [
        ("East", 20),
        ("North", 10),
    ]
    big = _as(runtime.query, {**BY_STORE, "where": [BIG_STORES]}, customer_id=B)
    assert _revenue(big) == [("s1", 400), ("s3", 300)]
    assert _revenue(_as(runtime.query, {**BY_STORE, "where": [BIG_STORES]}, customer_id=A)) == []


def test_listing_a_model_no_filter_reaches_is_held(runtime):
    listing = {"select": [], "group_by": ["dimension.rf_store_store_name"]}
    assert _as(runtime.compile, listing, audience="internal")["ok"] is True
    assert _denied(lambda: _as(runtime.compile, listing, customer_id=A)) == (
        "row_filter_uncovered_root"
    )


@pytest.mark.parametrize(
    "query",
    [
        {  # a prior period over the series' calendar spine
            "select": [{"expression": "metric.rf.revenue_change", "as": "change"}],
            "time": MONTH,
        },
        {  # a filled series over the calendar spine
            "select": [{"expression": REVENUE, "as": "revenue"}],
            "time": {**MONTH, "grain": "day", "start": "2026-01-01", "fill": True},
        },
        {  # a named window answers every period over the same spine
            "select": [{"expression": REVENUE, "as": "revenue"}],
            "time": {**MONTH, "start": "2026-01-01", "end": "2026-04-01"},
        },
    ],
    ids=["dependency", "spine", "named_window"],
)
def test_a_calendar_spine_reads_no_relation_so_each_caller_gets_its_own_series(runtime, query):
    """The engine's day series generates dates from its bounds and reads no relation: every
    scan of the filtered relation still carries the caller's filter."""
    for customer in (A, B):
        own = {
            **query,
            "where": [{"field": "dimension.rf_order_customer_id", "op": "is", "value": customer}],
        }
        expected = _as(runtime.query, own, audience="internal", now="2026-06-01")["rows"]
        result = _as(runtime.query, query, customer_id=customer, now="2026-06-01")
        assert result["rows"] == expected
        sql = result["rendered_sql"]
        assert sql.count("FROM order_fact") == sql.count("order_fact.customer_id = ?") > 0


def test_segment_preview_and_count_bind_the_filter(runtime):
    context = _ctx(customer_id=A).to_policy_context()
    a = runtime.segment_preview("segment.rf.in_s1", policy_context=context)
    b = runtime.segment_preview(
        "segment.rf.in_s1", policy_context=_ctx(customer_id=B).to_policy_context()
    )
    assert (a["member_count"], b["member_count"]) == (1, 1) and "?" in a["count_sql"]
    assert (a["rows"], b["rows"]) == (
        [{"dimension.rf_order_order_ref": 1}],
        [{"dimension.rf_order_order_ref": 4}],
    )
    big = runtime.segment_preview("segment.rf.big_store", policy_context=context)
    assert (big["member_count"], big["rows"]) == (0, [])


def test_rollups_are_not_routed_under_a_row_filter(runtime):
    query = {"select": [{"expression": REVENUE, "as": "revenue"}], "time": MONTH}
    internal = _as(runtime.compile, query, audience="internal")
    filtered = _as(runtime.query, query, customer_id=B)
    assert "FROM order_monthly" in internal["rendered_sql"]
    assert internal["explain"]["performance_plan"]["aggregate_routing"]["selected"] == [
        "aggregate_relation.orders_monthly"
    ]
    assert (
        "FROM order_fact" in filtered["rendered_sql"]
        and "order_monthly" not in filtered["rendered_sql"]
    )
    assert sorted(row["revenue"] for row in filtered["rows"]) == [300, 400]


def test_zero_rows_report_only_the_callers_own_coverage(runtime):
    # An open window after the data: no row (a named one would answer its periods, NULL).
    window = {"select": [{"expression": REVENUE, "as": "revenue"}],
              "time": {**MONTH, "start": "2027-01-01"}}  # fmt: skip
    coverage = {
        tenant: _as(runtime.query, window, customer_id=tenant)["data_diagnostics"][
            "actual_data_coverage"
        ]
        for tenant in (A, B)
    }
    assert {tenant: (row["min"][:10], row["max"][:10]) for tenant, row in coverage.items()} == {
        A: ("2026-01-10", "2026-02-10"),
        B: ("2026-01-20", "2026-02-20"),
    }
    everyone = _as(runtime.query, window, audience="internal")["data_diagnostics"]
    assert everyone["actual_data_coverage"]["min"][:10] == "2026-01-10"


def test_driver_errors_never_echo_a_bound_value(runtime, caplog):
    with (
        caplog.at_level(logging.DEBUG, logger="semantic_rails"),
        pytest.raises(SemanticLayerError) as caught,
    ):
        _as(runtime.query, audience="mistyped", customer_id=A)
    error, chain = caught.value, []
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert chain[0].code == "QUERY_EXECUTION_ERROR"
    assert "ConversionException" in caplog.text
    assert A not in json.dumps([[str(e), getattr(e, "details", {})] for e in chain], default=str)
    assert A not in caplog.text


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"dimension": "dimension.rf_order_ordered_at"}, "has type 'timestamp'"),
        ({"dimension": "dimension.rf_order_store_id"}, "an id dimension needs 'type'"),
        ({"dimension": "dimension.nope"}, "must name a dimension"),
        ({"attribute": "Customer-ID"}, "attribute names must match"),
        ({"op": "!="}, "unknown key 'op'"),
        ({"object_ids": ["measure.rf.revenue"]}, "unknown key 'object_ids'"),
        ({"object_ids": []}, "unknown key 'object_ids'"),
        ({"action": ""}, "unknown key 'action'"),
        ({"type": "integer"}, "has type 'string'"),
        (
            {"dimension": "dimension.rf_order_ordered_at", "type": "uuid"},
            "has type 'timestamp'; a row filter compares a string, integer or boolean column, "
            "or a UUID column as type 'uuid'",
        ),
        ({"type": "UUID"}, "has type 'string'"),
        ({"kind": "row_filters"}, "looks like a row filter"),
        ({"kind": "Row-Filter"}, "looks like a row filter"),
        (
            {"kind": "RowFilter", "attribute": DROP, "attr": "customer_id"},
            "looks like a row filter",
        ),
        ({"kind": "object_access"}, "looks like a row filter"),  # a stray attribute
        ({"unless_roles": []}, "'unless_roles' must be a non-empty list of role names"),
        ({"unless_roles": "auditor"}, "'unless_roles' must be a non-empty list of role names"),
        ({"unless_roles": ["auditor", " "]}, "'unless_roles' must be a non-empty list"),
        ({"unless_role": ["auditor"]}, "unknown key 'unless_role'"),
        (
            {"roles": ["Auditor", "store"], "unless_roles": ["auditor"]},
            "role 'auditor' is in both 'roles' and 'unless_roles'",
        ),
    ],
)
def test_the_package_rejects_a_row_filter_it_cannot_enforce(tmp_path, change, problem):
    policy = {key: value for key, value in {**OWN_ORDERS, **change}.items() if value is not DROP}
    with pytest.raises(SemanticLayerError, match=re.escape(problem)):
        load_package_config(str(_package(tmp_path / "rf", [policy])))


@pytest.mark.parametrize(
    ("dimension", "slot_type"),
    [
        ("dimension.rf_order_store_id", "string"),
        ("dimension.rf_order_store_id", "uuid"),  # a key held as a UUID
    ],
)
def test_an_id_dimension_takes_an_explicit_type(tmp_path, dimension, slot_type):
    policy = {**OWN_ORDERS, "dimension": dimension, "type": slot_type,
              "rationale": "An id dimension needs an explicit type."}  # fmt: skip
    config = load_package_config(str(_package(tmp_path / "rf", [policy])))
    [row] = row_filters_for_context(config, _ctx(customer_id=UA).to_policy_context())
    assert row.slot == ParameterSlot("customer_id", slot_type)


def test_nested_row_filter_stays_refused_at_load(tmp_path):
    policy = {
        "id": "policy.rf.nested",
        "kind": "row_filter",
        "config": {"dimension": OWN_ORDERS["dimension"], "attribute": "customer_id"},
    }
    with pytest.raises(SemanticLayerError) as exc:
        load_package_config(str(_package(tmp_path / "rf", [policy])))
    assert exc.value.code == "INVALID_CONFIG"
    assert "config" in str(exc.value)


@pytest.mark.parametrize(
    ("fields", "key"),
    [({"object_ids": ["measure.rf.revenue"]}, "object_ids"), ({"action": "label"}, "action")],
)
@pytest.mark.parametrize("audiences", [[], ["external"]])
def test_a_row_filter_that_skipped_the_loader_still_fails_closed(runtime, fields, key, audiences):
    unscoped = {"dimension": OWN_ORDERS["dimension"], "attribute": "customer_id"}
    policy = SemanticPolicyConfig("p", "row_filter", unscoped, audiences=audiences, **fields)
    config = replace(runtime._config, semantic_policies=[policy])
    with pytest.raises(SemanticLayerError, match=f"unknown key '{key}'") as exc:
        row_filters_for_context(config, _ctx(audience="internal").to_policy_context())
    assert exc.value.code == "INVALID_CONFIG"


def test_a_pipeline_backed_relation_cannot_be_filtered(runtime):
    config = runtime._config
    entities = [replace(row, relation_id="relation.rf.orders") for row in config.entities]
    with pytest.raises(SemanticLayerError, match="must be a plain column"):
        row_filters_for_context(replace(config, entities=entities), _ctx().to_policy_context())


def test_two_filters_on_one_relation_bind_in_placeholder_order(runtime):
    context = RequestContext(audience="customer", roles=("store_scoped",),
                             attributes={"customer_id": B, "store_id": "s1"})  # fmt: skip
    result = runtime.query(BY_STORE, context=context)
    assert [row["revenue"] for row in result["rows"]] == [400]
    assert "order_fact.customer_id = ? AND order_fact.store_id = ?" in result["rendered_sql"]


def _stores(runtime, **attributes):
    """Revenue by store for an internal caller whose role scopes it to ``store_id``."""
    context = RequestContext(audience="internal", roles=("store_scoped",), attributes=attributes)
    return runtime.query(BY_STORE, context=context)


def _revenue(result):
    return [(row["dimension.rf_order_store_id"], row["revenue"]) for row in result["rows"]]


def test_a_list_attribute_reads_any_of_its_values(runtime, monkeypatch):
    adapter, bound = runtime._get_adapter(), []
    real = adapter.query_prepared

    def record(prepared, **kwargs):
        bound.append(tuple(kwargs.get("parameters", ())))
        return real(prepared, **kwargs)

    monkeypatch.setattr(adapter, "query_prepared", record)
    one, other = _stores(runtime, store_id="s1"), _stores(runtime, store_id="s3")
    both = _stores(runtime, store_id=["s1", "s3"])
    assert _revenue(both) == sorted(_revenue(one) + _revenue(other)) == [("s1", 410), ("s3", 300)]
    assert "order_fact.store_id = ?" in one["rendered_sql"]  # a scalar is unchanged
    assert "(order_fact.store_id IN (?, ?))" in both["rendered_sql"]
    assert not re.search(r"'s[13]'", both["rendered_sql"])
    assert bound[-1] == ("s1", "s3")


def test_two_list_filters_bind_each_value_to_its_own_placeholder(runtime):
    context = RequestContext(audience="customer", roles=("store_scoped",),
                             attributes={"customer_id": [B, A], "store_id": ["s2", "s1"]})  # fmt: skip
    result = runtime.query(BY_STORE, context=context)
    assert _revenue(result) == [("s1", 410), ("s2", 20)]
    assert (
        "(order_fact.customer_id IN (?, ?)) AND (order_fact.store_id IN (?, ?))"
        in result["rendered_sql"]
    )
    # Every engine probe of a named window carries both lists.
    query = {
        "select": [{"expression": REVENUE, "as": "v"}],
        "time": {**MONTH, "start": "2026-01-01", "end": "2026-02-01"},
    }
    context = replace(context, attributes={"customer_id": [A, B], "store_id": ["s1", "s3"]})
    result = runtime.query(query, context=context)
    assert result["rows"][0]["v"] == 310
    assert result["rendered_sql"].count("customer_id IN (?, ?)") == 3
    assert result["rendered_sql"].count("store_id IN (?, ?)") == 3


def test_list_order_and_length_never_share_a_plan(runtime, monkeypatch):
    adapter, bound = runtime._get_adapter(), []
    real = adapter.query_prepared

    def record(prepared, **kwargs):
        bound.append(tuple(kwargs.get("parameters", ())))
        return real(prepared, **kwargs)

    monkeypatch.setattr(adapter, "query_prepared", record)

    def ask(value, *verified):
        attributes = TrustedAttributes({"store_id": value}, roles=verified)
        context = RequestContext(
            audience="internal", roles=("store_scoped",), attributes=attributes
        )
        result = runtime.query(BY_STORE, context=context, verbosity="full")
        return _revenue(result), result["compile_stats"]["cache_hit"], bound[-1]

    s1, s2, s3 = ("s1", 410), ("s2", 20), ("s3", 300)
    asked = [
        (["s1", "s3"], (), [s1, s3], ("s1", "s3")),
        ("s1", (), [s1], ("s1",)),
        (["s3", "s1"], (), [s1, s3], ("s3", "s1")),
        (["s1"], (), [s1], ("s1",)),
        (["s1", "s3"], ("store_auditor",), [s1, s2, s3], ()),  # exempt: no filter at all
    ]
    for warm in (False, True):  # the second round repeats each request after the others
        for value, verified, rows, values in asked:
            assert ask(value, *verified) == (rows, warm, values), (value, verified, warm)


def test_an_empty_list_is_denied_at_the_request(runtime, monkeypatch):
    monkeypatch.setattr(runtime_module, "_adapter_query", _forbidden)
    context = _ctx(customer_id=[])  # a grant of no values is a context, not a TypeError
    preview = context.to_policy_context()
    with pytest.raises(SemanticLayerError) as caught:
        runtime.compile(BY_STORE, context=context)
    assert caught.value.details == {"reason": "empty_attribute_list", "attribute": "customer_id"}
    reasons = {
        _denied(lambda: runtime.query(BY_STORE, context=context)),
        _denied(lambda: runtime.segment_preview("segment.rf.in_s1", policy_context=preview)),
        runtime.validate(BY_STORE, context=context)["errors"][0]["details"]["reason"],
    }
    assert reasons == {"empty_attribute_list"}
    # Only a filter that reads the attribute denies: an internal caller's list goes unused.
    monkeypatch.undo()
    internal = _as(runtime.query, audience="internal", customer_id=[])
    assert _revenue(internal) == [("s1", 410), ("s2", 20), ("s3", 300)]


def test_a_mixed_type_list_is_denied():
    # A list mixes no types (1 and True included): the host's context is refused as built.
    for mixed in ([A, 7], [1, True]):
        with pytest.raises(TypeError):
            _ctx(customer_id=mixed)
    # A list of another type than the filter's is denied at the request, item by item.
    listed = RowFilter("p", "t", "c", ParameterSlot("customer_id", "string"))
    assert _denied(lambda: listed.for_value((A, 7))) == "attribute_type_mismatch"
    assert _denied(lambda: listed.for_value((7,))) == "attribute_type_mismatch"


def test_a_scalar_slot_never_binds_a_list():
    # A boolean filter takes one value, as before; a list never reaches its slot.
    flag = RowFilter("p", "t", "c", ParameterSlot("active", "boolean"))
    assert flag.for_value(True) == flag
    assert _denied(lambda: flag.for_value((True,))) == "attribute_type_mismatch"
    with pytest.raises(ValueError):
        ParameterSlot("active", "boolean", index=0)
    for index in (-1, True, "0"):
        with pytest.raises(ValueError):
            ParameterSlot("customer_id", "string", index=index)  # type: ignore[arg-type]
    listed = RowFilter("p", "t", "c", ParameterSlot("customer_id", "string")).for_value((A, B))
    assert [slot.index for slot in listed.slots] == [0, 1]
    filtered, slots = _apply(_scan(SqlTableRef("t")), listed)
    assert slots == listed.slots
    assert render_select(filtered).endswith("WHERE\n  (t.c IN (?, ?))")


def test_a_uuid_filter_reads_any_form_of_the_id_as_its_lower_case_text(tmp_path, monkeypatch):
    # The customer ids are UUID text in a VARCHAR column, which DuckDB compares as text: only
    # the lower-case hyphenated form a host's value binds as finds them.
    root = _package(tmp_path / "rf", [{**OWN_ORDERS, "type": "uuid"}])
    with duckdb.connect(str(root / "rf.duckdb")) as connection:
        connection.execute(SEED.replace(A, UA).replace(B, UB))
    runtime = Runtime.from_path(str(root))
    adapter, bound = runtime._get_adapter(), []
    real = adapter.query_prepared

    def record(prepared, **kwargs):
        bound.append(tuple(kwargs.get("parameters", ())))
        return real(prepared, **kwargs)

    monkeypatch.setattr(adapter, "query_prepared", record)
    forms = [UA, UA.upper(), "{" + UA + "}", UA.replace("-", "").upper()]
    for form in forms:
        result = _as(runtime.query, customer_id=form)
        assert _revenue(result) == [("s1", 10), ("s2", 20)], form
        assert "order_fact.customer_id = ?" in result["rendered_sql"]  # no cast on DuckDB
    assert bound == [(UA,)] * len(forms)
    both = _as(runtime.query, customer_id=[UB.upper(), UA])
    assert _revenue(both) == [("s1", 410), ("s2", 20), ("s3", 300)]
    assert bound[-1] == (UB, UA)
    runtime.close()


@pytest.mark.parametrize(
    "value",
    [
        "cust-a",
        UA[:-1],  # one digit short
        UA + "0",
        "{" + UA,  # an unclosed brace
        UA + "}",
        " " + UA,
        UA + "\n",
        UA.replace("-", "_"),
        UA.replace("-", "", 1),  # hyphens in some places only
        "g" + UA[1:],  # not a hex digit
        7,
        True,
    ],
)
def test_a_uuid_filter_denies_a_value_that_is_not_a_uuid(value):
    row = RowFilter("p", "t", "c", ParameterSlot("customer_id", "uuid"))
    assert _denied(lambda: row.for_value(value)) == "attribute_type_mismatch"
    assert _denied(lambda: row.for_value((UA, value))) == "attribute_type_mismatch"
    prepared = sql_preparation.PreparedQuery("SELECT 1 WHERE c = ?", parameters=(row.slot,))
    with pytest.raises(SemanticLayerError) as caught:
        sql_preparation.checked_parameter_values(prepared, [value])
    assert caught.value.code == "POLICY_DENIED"
    assert str(caught.value) == "Query parameters could not be bound safely for this request."
    assert caught.value.details == {"reason": "attribute_type_mismatch", "attribute": "customer_id"}


def test_a_uuid_parameter_is_cast_only_where_text_never_compares_with_a_uuid_column():
    row = RowFilter("p", "t", "c", ParameterSlot("customer_id", "uuid"))
    text = RowFilter("p", "t", "s", ParameterSlot("store_id", "string"))
    listed = row.for_value((UA, UB))
    statement = _scan(SqlTableRef("t"))
    rendered = {}
    for warehouse in ("postgres", "duckdb", "snowflake"):
        dialect = dialect_for_warehouse(warehouse)
        sql, slots = render_with_slots(
            apply_row_filters(statement, Visibility((row, text), modeled=frozenset({"t"}))),
            dialect=dialect,
        )
        connection = {"postgres": "postgres_native", "snowflake": "snowflake_adbc"}
        prepared = sql_preparation.finalize_parameters(
            sql_preparation.PreparedQuery(sql, parameters=slots), connection.get(warehouse, "")
        )
        listed_sql, _ = render_with_slots(
            apply_row_filters(statement, Visibility((listed,), modeled=frozenset({"t"}))),
            dialect=dialect,
        )
        rendered[warehouse] = tuple(
            " ".join(each.split("WHERE")[1].split()) for each in (prepared.sql, listed_sql)
        )
    # Only the trusted value is cast, never the column, and a string slot never is.
    assert rendered["postgres"] == (
        "t.c = CAST($1 AS UUID) AND t.s = $2",
        "(t.c IN (CAST(? AS UUID), CAST(? AS UUID)))",
    )
    assert rendered["duckdb"] == rendered["snowflake"] == ("t.c = ? AND t.s = ?", "(t.c IN (?, ?))")


def test_unless_roles_exempts_only_the_named_roles(tmp_path):
    root = _package(tmp_path / "rf", [{**OWN_ORDERS, "unless_roles": ["auditor"]}])
    with duckdb.connect(str(root / "rf.duckdb")) as connection:
        connection.execute(SEED)
    runtime = Runtime.from_path(str(root))

    def ask(*roles, verified=(), **attributes):
        """``roles`` as the context claims them; ``verified`` as the host verified them."""
        trusted = TrustedAttributes(attributes, roles=verified)
        context = RequestContext(audience="customer", roles=roles, attributes=trusted)
        return runtime.query(BY_STORE, context=context)

    everyone = [("s1", 410), ("s2", 20), ("s3", 300)]
    assert _revenue(ask(verified=["auditor"])) == everyone
    assert _revenue(ask("analyst", verified=["analyst", "Auditor"], customer_id=A)) == everyone
    assert "?" not in ask(verified=["auditor"])["rendered_sql"]
    # A role the context only claims, as a request header would, exempts no one.
    assert _revenue(ask("auditor", customer_id=A)) == [("s1", 10), ("s2", 20)]
    assert _revenue(ask(verified=["analyst"], customer_id=A)) == [("s1", 10), ("s2", 20)]
    assert _denied(lambda: ask("auditor")) == _denied(lambda: ask()) == "missing_attribute"


class _HostResolver:
    """An embedding host: audience and roles as request headers claim them, attributes (and
    any verified roles) from the caller's verified identity."""

    def __init__(self, attributes):
        self.attributes = attributes

    def resolve(self, headers, *, payload=None, request_id=""):
        claimed = context_from_headers(headers, request_id=request_id)
        return replace(claimed, attributes=self.attributes)


@pytest.fixture
def exempt_app(tmp_path, monkeypatch):
    """A row filter that exempts auditors, served over REST and MCP behind an API key."""
    root = _package(tmp_path / "rf", [{**OWN_ORDERS, "unless_roles": ["auditor"]}])
    with duckdb.connect(str(root / "rf.duckdb")) as connection:
        connection.execute(SEED)
    monkeypatch.setenv("SEMANTIC_RAILS_API_KEYS", "test-row-filter-key")
    previous = get_policy_context_resolver()
    app = SemanticLayerASGIApp(path=str(root), max_workers=1)

    def call(resolver, transport):
        set_policy_context_resolver(resolver)
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer test-row-filter-key",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "X-Semantic-Audience": "customer",
            "X-Semantic-Roles": "auditor",
        }
        path, body = "/api/v1/query", {"query": RUN["query"]}
        if transport == "mcp":
            params = {"name": "execute", "arguments": RUN}
            path, body = (
                "/mcp",
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
            )

        async def request():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return (await client.post(path, json=body, headers=headers)).json()

        response = asyncio.run(request())
        return response["result"]["structuredContent"] if transport == "mcp" else response

    yield call
    asyncio.run(app.aclose())
    set_policy_context_resolver(previous)


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_a_role_a_request_header_claims_never_lifts_a_row_filter(exempt_app, transport):
    claimed = exempt_app(HeaderPolicyContextResolver(), transport)
    assert claimed["ok"] is False and "rows" not in claimed
    assert claimed["errors"][0]["details"]["reason"] == "missing_attribute"
    # A host that passes the header's roles through, without verifying them: still filtered.
    own = exempt_app(_HostResolver(TrustedAttributes({"customer_id": A})), transport)
    assert sorted(_revenue(own)) == [("s1", 10), ("s2", 20)]
    # The host verified the role: the same request, now exempt (and never the cached plan).
    verified = TrustedAttributes({"customer_id": A}, roles=["auditor"])
    exempt = exempt_app(_HostResolver(verified), transport)
    assert sorted(_revenue(exempt)) == [("s1", 410), ("s2", 20), ("s3", 300)]


def test_parameters_beyond_the_warehouse_limit_refuse_before_execution(runtime, monkeypatch):
    class Untouchable:
        def __enter__(self):
            raise AssertionError("the request reached the query lock")

        def __exit__(self, *exc):
            return False

    connection_kind = runtime._config.package.connection.kind
    monkeypatch.setitem(sql_preparation.MAX_PARAMETERS, connection_kind, 3)
    monkeypatch.setattr(runtime, "_query_lock", Untouchable())
    regions = [f"s{index}" for index in range(4)]
    for method in (runtime.compile, runtime.query):
        context = RequestContext(roles=("store_scoped",), attributes={"store_id": regions})
        with pytest.raises(SemanticLayerError) as caught:
            method(BY_STORE, context=context)
        assert caught.value.code == "POLICY_DENIED"
        assert caught.value.details == {"reason": "parameter_limit_exceeded", "limit": 3}
        assert "s3" not in str(caught.value) and "4" not in str(caught.value)


def test_validate_warns_that_a_role_scoped_filter_leaves_other_roles_unfiltered(package):
    report, _ = parse_config_report(resolve_package_reference(path=str(package)))
    warnings = [row for row in report["warnings"] if row["code"] == "ROW_FILTER_SCOPED_BY_ROLE"]
    assert [row["details"] for row in warnings] == [
        {"policy_id": "policy.rf.own_store", "roles": ["store_scoped"]}
    ]
    assert "unless_roles" in warnings[0]["message"]


ROW = RowFilter("p", "t", "c", ParameterSlot("customer_id", "string"))


def _scan(source, joins=(), where=(), ctes=()):
    return SqlSelect([SqlField(SqlIdentifier(["v"]), "v")], source, list(joins), list(where),
                     ctes=list(ctes))  # fmt: skip


def _apply(statement, *filters, modeled=("t", "other", "parents", "grandchildren")):
    """``statement`` held to ``filters``, rendered: (statement, slots in render order)."""
    filtered = apply_row_filters(statement, Visibility(filters, modeled=frozenset(modeled)))
    return filtered, render_with_slots(filtered)[1]


@pytest.mark.parametrize(
    "statement",
    [
        _scan(SqlTableFunction("UNNEST", alias="t")),
        _scan(SqlTableRef("unmodeled")),  # a relation no model reads
        _scan(SqlTableRef("t"), [SqlJoin("RIGHT", SqlTableRef("other"), SqlLiteral(True))]),
        _scan(SqlTableRef("t"), ctes=[SqlCte("t", _scan(SqlTableRef("raw")))]),
    ],
    ids=["table_function", "unmodeled_relation", "right_join", "filtered_name_is_a_cte"],
)  # fmt: skip
def test_reads_the_filter_cannot_be_proven_for_are_refused(statement):
    assert _denied(lambda: _apply(statement, ROW)) == "row_filter_unsupported_query"


@pytest.mark.parametrize(
    ("statement", "reads"),
    [
        (_scan(SqlTableRef("other"), [SqlJoin("INNER", SqlTableRef("t"), SqlLiteral(True))]), 1),
        (_scan(SqlTableRef("t"), where=[SqlExists(_scan(SqlTableRef("t")))]), 2),
        (_scan(SqlTableRef("u"), ctes=[SqlCte("u", SqlSetQuery("UNION ALL", [_scan(SqlTableRef("t"))] * 2))]), 2),
        (SqlSelect([SqlField(SqlExists(_scan(SqlTableRef("t"))), "value")], SqlTableRef("parents")), 1),
        (_scan(SqlTableRef("parents"), where=[SqlExists(_scan(SqlTableRef("t")), negated=True)]), 1),
    ],
    ids=["joined", "second_read_in_exists", "set_query_branches", "projected_exists", "not_exists"],
)  # fmt: skip
def test_every_read_of_a_filtered_relation_is_filtered_where_it_is(statement, reads):
    filtered, slots = _apply(statement, ROW)
    assert (
        slots
        == (ROW.slot,) * reads
        == tuple(node.slot for node in sql_nodes(filtered) if isinstance(node, SqlParameter))
    )


def test_a_read_inside_a_cte_is_filtered_there():
    statement = _scan(SqlTableRef("c"), ctes=[SqlCte("c", _scan(SqlTableRef("t")))])
    filtered, slots = _apply(statement, ROW)
    assert slots == (ROW.slot,)
    assert "FROM t\nWHERE\n  t.c = ?\n)" in render_select(filtered)
    shadowed = RowFilter("p2", "c", "c", ROW.slot)  # its relation's name is the CTE's
    assert _denied(lambda: _apply(statement, ROW, shadowed)) == "row_filter_unsupported_query"


@pytest.mark.parametrize(
    "statement",
    [
        # An unmodeled physical scan cannot hide beside an EXISTS scan.
        _scan(
            SqlTableRef("parents"),
            where=[SqlExists(_scan(SqlTableRef("t")))],
            ctes=[SqlCte("extra", _scan(SqlTableRef("extra_rows")))],
        ),
        # Nor inside one.
        _scan(
            SqlTableRef("parents"),
            where=[
                SqlExists(
                    _scan(
                        SqlTableRef("t"),
                        joins=[SqlJoin("INNER", SqlTableRef("unmodeled"), SqlLiteral(True))],
                    )
                )
            ],
        ),
        _scan(
            SqlTableRef("parents"), where=[SqlExists(_scan(SqlTableFunction("UNNEST", alias="t")))]
        ),
    ],
    ids=["extra_scan", "joined_child", "table_function"],
)
def test_exists_does_not_bypass_row_filter_scan_guard(statement):
    assert _denied(lambda: _apply(statement, ROW)) == "row_filter_unsupported_query"


def test_a_rewrite_that_misses_the_scan_is_denied(monkeypatch):
    monkeypatch.setattr(row_filters_module, "_add_predicates", lambda select, *_: select)
    assert _denied(lambda: _apply(_scan(SqlTableRef("t")), ROW)) == "row_filter_unsupported_query"


@pytest.fixture
def mcp(package, monkeypatch):
    """Call MCP tools as an embedding host whose resolver supplies each request's context."""
    context, events = {}, []

    class HostResolver:
        def resolve(self, headers, *, payload=None, request_id=""):
            return context["value"]

    class Sink:
        def emit(self, payload):
            events.append(payload)

    monkeypatch.setenv("SEMANTIC_RAILS_API_KEYS", "test-row-filter-key")
    monkeypatch.setenv("SEMANTIC_RAILS_AUDIT_LOGS", "1")
    previous = get_policy_context_resolver(), get_audit_sink()
    set_policy_context_resolver(HostResolver())
    set_audit_sink(Sink())
    app = SemanticLayerASGIApp(path=str(package), max_workers=1)

    async def request(tool, arguments):
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

    def call(request_context, tool, arguments):
        context["value"] = request_context
        return asyncio.run(request(tool, arguments))

    yield call, events
    asyncio.run(app.aclose())
    set_policy_context_resolver(previous[0])
    set_audit_sink(previous[1])


RUN = {"query": {key: value for key, value in BY_STORE.items() if key != "order_by"}, "mode": "run"}


def test_every_mcp_surface_shows_each_customer_only_their_rows(mcp):
    """Plan, execute, segment preview and live valid-values through the MCP transport."""
    call, events = mcp

    def surfaces(customer_id):
        context = _ctx(customer_id=customer_id)
        out = {"plan": call(context, "plan", {"intent": "revenue by store"})}
        for mode in ("validate", "sql", "run"):
            out[mode] = call(context, "execute", {**RUN, "mode": mode})
        preview = {"action": "preview", "segment_id": "segment.rf.in_s1"}
        out["preview"] = call(context, "segment", preview)
        values = {"dimension_id": "dimension.rf_order_order_ref", "allow_live_query": True}
        out["values"] = call(context, "valid-values", values)
        return out

    a, b = surfaces(A), surfaces(B)
    assert all(response["ok"] is True for response in [*a.values(), *b.values()]), (a, b)
    assert a["run"]["rows"] != b["run"]["rows"] and a["preview"]["rows"] != b["preview"]["rows"]
    assert [row["value"] for row in a["values"]["values"]] == [1, 2]
    assert [row["value"] for row in b["values"]["values"]] == [3, 4]
    assert B not in json.dumps(a, default=str) and A not in json.dumps(b, default=str)
    assert A not in json.dumps(events, default=str) and B not in json.dumps(events, default=str)


def test_mcp_denies_an_empty_list_as_a_policy_refusal(mcp):
    call, _ = mcp
    response = call(_ctx(customer_id=[]), "execute", RUN)
    assert response["ok"] is False and response["error"]["code"] == "POLICY_DENIED"
    assert response["errors"][0]["details"]["reason"] == "empty_attribute_list"


def test_mcp_denies_a_missing_attribute_and_audits_failures_without_values(mcp):
    call, events = mcp
    missing = call(_ctx(), "execute", RUN)
    failed = call(_ctx(audience="mistyped", customer_id=A), "execute", RUN)
    assert (missing["ok"], failed["ok"]) == (False, False)
    codes = [code for event in events for code in event.get("error_codes", [])]
    assert codes == ["POLICY_DENIED", "QUERY_EXECUTION_ERROR"]
    assert A not in json.dumps([missing, failed, events], default=str)


@pytest.mark.parametrize("tenant", [A, B, "customer-with-no-rows"])
@pytest.mark.parametrize("window_total", [False, True])
def test_observation_and_coverage_never_see_another_customers_rows(runtime, tenant, window_total):
    count = "measure.rf.s2_count"
    query = {
        "select": [{"expression": REVENUE, "as": "revenue"}, {"expression": count, "as": "v"}],
        "time": {**MONTH, "start": "2026-01-01", "end": "2026-02-01"},
    }
    if window_total:
        query["time"].pop("grain")
    result = _as(runtime.query, query, customer_id=tenant)
    # A named window answers its month, or its total, for every caller. Both A and B have
    # source rows, and the loaded-bucket check gates the window's bucket, so B's
    # never-matched condition counts zero; an empty caller reads NULL (nothing in its scope).
    gold = (
        runtime._get_adapter()
        ._db.conn.execute(
            "SELECT COUNT(CASE WHEN store_id = 's2' THEN order_id END) FROM order_fact "
            "WHERE customer_id = ? AND ordered_at >= TIMESTAMP '2026-01-01' "
            "AND ordered_at < TIMESTAMP '2026-02-01' GROUP BY date_trunc('month', ordered_at)",
            [tenant],
        )
        .fetchall()
    )
    assert [r["v"] for r in result["rows"]] == ([0] if tenant in {A, B} else [None])
    assert gold == ([(0,)] if tenant in {A, B} else [])
    # Both measures are probed outside the bounds, plus the leaf and its coverage scan.
    assert result["rendered_sql"].count("customer_id = ?") == 4
    assert tenant not in result["rendered_sql"]


def test_a_tagged_row_returning_scan_is_filtered_too():
    extra = replace(_scan(SqlTableRef("t")), observation_scan=True)
    statement = replace(_scan(SqlTableRef("t")), where=[SqlExists(extra)])
    assert _apply(statement, ROW)[1] == (ROW.slot, ROW.slot)


def test_an_empty_tenant_never_borrows_another_tenants_observation(package):
    with duckdb.connect(str(package / "rf.duckdb")) as connection:
        connection.execute("DELETE FROM order_fact WHERE customer_id = ?", [A])
    runtime = Runtime.from_path(str(package))
    adapter = runtime._get_adapter()
    query = {
        "select": [
            {"expression": REVENUE, "as": "v"},
            {"expression": "measure.rf.order_count", "as": "n"},
        ],
        "where": [
            {"field": "dimension.rf_order_ordered_at", "op": "gte", "value": "2026-01-01"},
            {"field": "dimension.rf_order_ordered_at", "op": "lt", "value": "2026-02-01"},
        ],
    }
    gold = adapter._db.conn.execute(
        "SELECT SUM(amount), NULLIF(COUNT(order_id), 0) "
        "FROM order_fact WHERE customer_id = ? AND ordered_at >= TIMESTAMP '2026-01-01' "
        "AND ordered_at < TIMESTAMP '2026-02-01'",
        [A],
    ).fetchall()
    result = _as(runtime.query, query, customer_id=A)
    assert [(row["v"], row["n"]) for row in result["rows"]] == gold == [(None, None)]
    assert _as(runtime.query, query, customer_id=B)["rows"] == [{"v": 300, "n": 1}]


def test_every_tagged_probe_gets_both_policy_filters(runtime):
    query = {
        "select": [{"expression": REVENUE, "as": "v"}],
        "time": {**MONTH, "start": "2026-01-01", "end": "2026-02-01"},
    }
    context = replace(_ctx(customer_id=A, store_id="s1"), roles=("store_scoped",))
    result = runtime.query(query, context=context)
    assert result["rows"][0]["v"] == 10
    # The leaf, its observation outside the window and the named window's loaded coverage.
    assert result["rendered_sql"].count("customer_id = ?") == 3
    assert result["rendered_sql"].count("store_id = ?") == 3


def test_empty_tenant_coverage_and_observation_match_filtered_gold(package):
    # A bounded aggregate supplies one empty row; the real guard then reads both
    # engine probes. B's positive value must not make A's empty value become zero.
    dialect = dialect_for_warehouse("duckdb")
    stamp = SqlIdentifier(["order_fact", "ordered_at"])
    bucket = dialect.date_trunc("month", stamp)
    value = SqlCall("SUM", [SqlIdentifier(["order_fact", "amount"])])
    source = SqlSelect(
        select=[
            SqlField(dialect.timestamp_cast(SqlLiteral("2026-01-01")), "t"),
            SqlField(value, "v"),
            SqlField(SqlCall("COUNT", [SqlLiteral(1)]), "v_rows"),
        ],
        from_table=SqlTableRef("order_fact"),
        where=[
            SqlBinary(stamp, ">=", SqlLiteral("2026-01-01")),
            SqlBinary(stamp, "<", SqlLiteral("2026-02-01")),
        ],
    )
    scope = LeafScope(
        SqlTableRef("order_fact"),
        (),
        (),
        value,
        bucket=bucket,
        raw_time=stamp,
        bounded=True,
    )
    statement = SqlSelect(
        select=[SqlField(SqlIdentifier(["guarded_base", "v"]), "v")],
        from_table=SqlTableRef("guarded_base"),
        ctes=[
            SqlCte("source", source),
            *guard_empty_groups(
                "source",
                ["t"],
                ["v"],
                {"v": "sum"},
                {"v": scope},
                rows={"v": "v_rows"},
                time_key="t",
                dialect=dialect,
            ),
        ],
    )
    with duckdb.connect(str(package / "rf.duckdb")) as connection:
        connection.execute("DELETE FROM order_fact WHERE customer_id = ?", [A])
        for tenant, expected in [(A, [(None,)]), (B, [(300,)])]:
            filtered, slots = _apply(
                statement,
                RowFilter("own_orders", "order_fact", "customer_id", ROW.slot),
                RowFilter(
                    "own_store", "order_fact", "store_id", ParameterSlot("store_id", "string")
                ),
                modeled=("order_fact",),
            )
            sql = render_select(filtered)
            assert sql.count("customer_id = ?") == sql.count("store_id = ?") == 3
            assert len(slots) == 6
            got = connection.execute(sql, [tenant, "s3"] * 3).fetchall()
            gold = connection.execute(
                "SELECT SUM(amount) FROM order_fact WHERE customer_id = ? "
                "AND ordered_at >= TIMESTAMP '2026-01-01' "
                "AND ordered_at < TIMESTAMP '2026-02-01' AND store_id = 's3'",
                [tenant],
            ).fetchall()
            assert got == gold == expected
