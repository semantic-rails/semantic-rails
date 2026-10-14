"""Row filters through joins, routes and entity hops: each caller reads a visible database.

A row is visible when it passes its relation's own filters and every owner on its package
route to a filtered model is visible. Every read of a restricted relation carries that whole
predicate where it enters its scope, and a structural check refuses any statement where one
doesn't. Stores are filtered by region; orders and order lines follow their store; customers
belong to no store.
"""

from __future__ import annotations

from dataclasses import replace

import duckdb
import pytest
import yaml

from semantic_rails import row_filters as row_filters_module
from semantic_rails import runtime as runtime_module
from semantic_rails.compiler import compile_query
from semantic_rails.config_validation import parse_config_report, resolve_package_reference
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.policies import row_filters_for_context
from semantic_rails.renderer import render_with_slots
from semantic_rails.request_context import TrustedAttributes
from semantic_rails.row_filter_visibility import visibility_for
from semantic_rails.row_filters import (
    ParentHop,
    RowFilter,
    enforce_row_filters,
    verify_row_filters,
)
from semantic_rails.runtime import Runtime
from semantic_rails.sql_ast import (
    SqlBinary,
    SqlCte,
    SqlExists,
    SqlField,
    SqlIdentifier,
    SqlInQuery,
    SqlJoin,
    SqlLiteral,
    SqlSelect,
    SqlTableFunction,
    SqlTableRef,
)
from semantic_rails.sql_preparation import ParameterSlot

SEED = """
CREATE TABLE store_dim AS SELECT * FROM (VALUES
 ('s1', 'Elm', 'West', 100), ('s2', 'Oak', 'West', 200), ('s3', 'Pine', 'East', 300)
) t(store_id, store_name, region, sqft);
CREATE TABLE customer_dim AS SELECT * FROM (VALUES ('c1', 'Retail'), ('c2', 'Pro'), ('c3', 'Retail'))
 t(customer_id, segment);
CREATE TABLE order_fact AS SELECT * FROM (VALUES
 ('o1', 's1', 'c1', TIMESTAMP '2026-01-10', 10), ('o2', 's1', 'c2', TIMESTAMP '2026-02-10', 20),
 ('o3', 's2', 'c1', TIMESTAMP '2026-02-11', 30), ('o4', 's3', 'c1', TIMESTAMP '2026-01-12', 400),
 ('o5', 's3', 'c3', TIMESTAMP '2026-02-13', 500), ('o6', NULL, 'c2', TIMESTAMP '2026-02-14', 7),
 ('o7', 's9', 'c3', TIMESTAMP '2026-01-15', 8)
) t(order_id, store_id, customer_id, ordered_at, amount);
CREATE TABLE order_line AS SELECT * FROM (VALUES ('l1', 'o1', 1), ('l2', 'o1', 2), ('l3', 'o4', 5),
 ('l4', 'o3', 1)) t(line_id, order_id, qty);
CREATE TABLE calendar_day AS SELECT d::DATE AS date_day FROM
 range(DATE '2026-01-01', DATE '2026-04-01', INTERVAL 1 DAY) t(d);
"""
REGION = {
    "id": "policy.rj.region",
    "kind": "row_filter",
    "dimension": "dimension.rj_store_region",
    "attribute": "region",
    "audiences": ["regional"],
    "unless_roles": ["auditor"],
}
# A second filter, for callers with this role: customers by segment.
SEGMENT = {
    "id": "policy.rj.segment",
    "kind": "row_filter",
    "dimension": "dimension.rj_customer_segment",
    "attribute": "segment",
    "audiences": [],
    "roles": ["segment_scoped"],
}
REVENUE = {"kind": "aggregate", "measure": "measure.rj.revenue", "aggregation": "sum"}
QTY = {"kind": "aggregate", "measure": "measure.rj.qty", "aggregation": "sum"}
SQFT = {"kind": "aggregate", "measure": "measure.rj.sqft", "aggregation": "sum"}
# Stores with at least 25 in revenue: an owner filtered by its children.
BIG_STORES = {
    "entity": "entity.rj_store",
    "having": [{"field": "measure.rj.revenue", "op": "gte", "value": 25}],
    "over": "all_time",
}
ORDERS = {"kind": "aggregate", "measure": "measure.rj.order_count", "aggregation": "count"}


def _put(root, name, doc):
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _package(root, policies=(REGION, SEGMENT)):
    time = {"column": "ordered_at", "kind": "timestamp", "class": "event_time", "default": True}
    _put(root, "package.yml", {
        "schema_version": 1,
        "package": {"id": "rj", "name": "rj", "description": "Row filters through joins",
                    "warehouse": "duckdb", "default_db": "rj.duckdb", "seed": {"kind": "external"}},
        "defaults": {"time": {"timezone": "UTC"}},
    })  # fmt: skip
    many_to_one = {"cardinality": "many_to_one"}
    _put(root, "graph.yml", {"graph": {
        "entities": {
            "order": {"key": ["order_id"], "model": "orders"},
            "store": {"key": ["store_id"], "model": "stores"},
            "customer": {"key": ["customer_id"], "model": "customers"},
            "line": {"key": ["line_id"], "model": "lines"},
            "time": {"kind": "time", "key": ["date_day"], "model": "calendar",
                     "allowed_as_root": False},
        },
        "relationships": {
            "orders_store": {"id": "relationship.orders_store", "entities": ["order", "store"],
                             **many_to_one},
            "orders_customer": {"id": "relationship.orders_customer",
                                "entities": ["order", "customer"], **many_to_one},
            "lines_order": {"id": "relationship.lines_order", "entities": ["line", "order"],
                            **many_to_one},
        },
    }})  # fmt: skip
    _put(root, "models/orders.yml", {"model": {
        "id": "orders", "relation": "order_fact",
        "entities": {"order": {}, "store": {}, "customer": {}},
        "times": {"ordered_at": time},
        "dimensions": {"order_ref": {"column": "order_id", "kind": "categorical"}},
        "measures": {
            "revenue": {"kind": "aggregate", "expr": "amount", "rollup": "additive"},
            "order_count": {"kind": "entity_count", "entity_key": "order_id", "rollup": "additive"},
        },
    }})  # fmt: skip
    _put(root, "models/stores.yml", {"model": {
        "id": "stores", "relation": "store_dim", "entities": {"store": {}},
        "dimensions": {"store_name": {"kind": "categorical", "domain": ["Elm", "Oak", "Pine"]},
                       "region": {"kind": "categorical"}},
        "measures": {"sqft": {"kind": "aggregate", "expr": "sqft", "rollup": "additive"}},
    }})  # fmt: skip
    _put(root, "models/customers.yml", {"model": {
        "id": "customers", "relation": "customer_dim", "entities": {"customer": {}},
        "dimensions": {"segment": {"kind": "categorical", "domain": ["Pro", "Retail"]}},
        "measures": {"customer_count": {"kind": "entity_count", "entity_key": "customer_id",
                                        "rollup": "additive"}},
    }})  # fmt: skip
    _put(root, "models/lines.yml", {"model": {
        "id": "lines", "relation": "order_line", "entities": {"line": {}, "order": {}},
        "measures": {"qty": {"kind": "aggregate", "expr": "qty", "rollup": "additive"}},
    }})  # fmt: skip
    _put(root, "models/calendar.yml", {"model": {
        "id": "calendar", "relation": "calendar_day", "calendar_id": "default",
        "entities": {"time": {}},
        "times": {"date_day": {"column": "date_day", "kind": "date", "class": "calendar_time"}},
    }})  # fmt: skip
    _put(root, "metrics/core.yml", {"metrics": {
        "rj.revenue": {"as": "metric.rj.revenue", "kind": "aggregate", "label": "Revenue",
                       "measure": "measure.rj.revenue", "aggregation": "sum",
                       "temporal_role": "temporal_role.rj_order_ordered_at",
                       "value_type": "number"},
    }})  # fmt: skip
    _put(root, "policies.yml", {"semantic_policies": list(policies)})
    return root


@pytest.fixture
def package(tmp_path):
    root = _package(tmp_path / "rj")
    with duckdb.connect(str(root / "rj.duckdb")) as connection:
        connection.execute(SEED)
    return root


@pytest.fixture
def runtime(package):
    return Runtime.from_path(str(package))


def _context(region=None, roles=(), audience="regional", **attributes):
    if region is not None:
        attributes["region"] = region
    return RequestContext(
        actor="end-user", audience=audience, roles=tuple(roles), attributes=attributes
    )


def _query(select, group_by=(), **extra):
    query = {"select": [{"expression": expr, "as": alias} for alias, expr in select.items()]}
    if group_by:
        query["group_by"] = list(group_by)
        query["order_by"] = [{"field": field, "direction": "asc"} for field in group_by]
    return {**query, **extra}


def _rows(result):
    return [tuple(row.values()) for row in result["rows"]]


def _denied(call):
    with pytest.raises(SemanticLayerError) as caught:
        call()
    assert caught.value.code == "POLICY_DENIED", caught.value
    return caught.value.details["reason"]


WEST = _context("West")
EAST = _context("East")
EVERYONE = _context(audience="internal")


def test_total_revenue_counts_only_orders_of_visible_stores(runtime):
    query = _query({"revenue": REVENUE})
    assert _rows(runtime.query(query, context=WEST)) == [(60,)]
    assert _rows(runtime.query(query, context=EAST)) == [(900,)]
    assert _rows(runtime.query(query, context=EVERYONE)) == [(975,)]


def test_revenue_by_store_shows_only_visible_stores(runtime):
    query = _query({"revenue": REVENUE}, ["dimension.rj_store_store_name"])
    assert _rows(runtime.query(query, context=WEST)) == [("Elm", 30), ("Oak", 30)]
    unfiltered = runtime.query(query, context=EVERYONE)
    assert sorted(_rows(unfiltered), key=str) == sorted(
        [("Elm", 30), ("Oak", 30), ("Pine", 900), (None, 15)], key=str
    )


def test_revenue_by_an_uncovered_lookup_keeps_visible_orders(runtime):
    query = _query({"revenue": REVENUE}, ["dimension.rj_customer_segment"])
    assert _rows(runtime.query(query, context=WEST)) == [("Pro", 20), ("Retail", 40)]


def test_child_to_owner_at_two_hops(runtime):
    query = _query({"qty": QTY}, ["dimension.rj_store_store_name"])
    assert _rows(runtime.query(query, context=WEST)) == [("Elm", 3), ("Oak", 1)]
    assert _rows(runtime.query(_query({"qty": QTY}), context=WEST)) == [(4,)]


def test_an_owner_filtered_by_its_visible_children(runtime):
    query = {**_query({"sqft": SQFT}), "where": [BIG_STORES]}
    assert _rows(runtime.query(query, context=EVERYONE)) == [(600,)]
    assert _rows(runtime.query(query, context=WEST)) == [(300,)]
    assert _rows(runtime.query(query, context=EAST)) == [(300,)]


def test_a_hidden_duplicate_owner_key_moves_nothing(package):
    with duckdb.connect(str(package / "rj.duckdb")) as connection:
        connection.execute("INSERT INTO store_dim VALUES ('s1', 'Secret', 'East', 999)")
    runtime = Runtime.from_path(str(package))
    by_store = _query({"revenue": REVENUE}, ["dimension.rj_store_store_name"])
    assert _rows(runtime.query(_query({"revenue": REVENUE}), context=WEST)) == [(60,)]
    assert _rows(runtime.query(by_store, context=WEST)) == [("Elm", 30), ("Oak", 30)]
    assert _rows(runtime.query(_query({"sqft": SQFT}), context=WEST)) == [(300,)]
    # East sees s1's orders under its own copy of s1.
    assert _rows(runtime.query(by_store, context=EAST)) == [("Pine", 900), ("Secret", 30)]


def test_a_child_with_two_filtered_parents_needs_both(runtime):
    context = _context("West", roles=["segment_scoped"], segment="Retail")
    assert _rows(runtime.query(_query({"revenue": REVENUE}), context=context)) == [(40,)]
    by_store = _query({"revenue": REVENUE}, ["dimension.rj_store_store_name"])
    assert _rows(runtime.query(by_store, context=context)) == [("Elm", 10), ("Oak", 30)]


def test_a_list_attribute_and_an_exempt_role(runtime):
    both = _context(("West", "East"))
    assert _rows(runtime.query(_query({"revenue": REVENUE}), context=both)) == [(960,)]
    verified = TrustedAttributes({"region": "West"}, roles=["auditor"])
    auditor = RequestContext(audience="regional", attributes=verified)
    assert _rows(runtime.query(_query({"revenue": REVENUE}), context=auditor)) == [(975,)]
    claimed = _context("West", roles=["auditor"])  # a role only claimed exempts no one
    assert _rows(runtime.query(_query({"revenue": REVENUE}), context=claimed)) == [(60,)]


def test_a_caller_with_no_visible_store_reads_null(runtime):
    north = _context("North")
    assert _rows(runtime.query(_query({"revenue": REVENUE}), context=north)) == [(None,)]
    by_store = _query({"revenue": REVENUE}, ["dimension.rj_store_store_name"])
    assert _rows(runtime.query(by_store, context=north)) == []


def test_a_root_no_filter_covers_is_held(runtime):
    query = _query({"customers": {"kind": "aggregate", "measure": "measure.rj.customer_count",
                                  "aggregation": "count_distinct"}})  # fmt: skip
    assert _rows(runtime.query(query, context=EVERYONE)) == [(3,)]
    assert _denied(lambda: runtime.query(query, context=WEST)) == "row_filter_uncovered_root"


def test_validate_lists_coverage_per_filter(package):
    report, _ = parse_config_report(resolve_package_reference(path=str(package)))
    coverage = {
        row["details"]["policy_id"]: row["details"]
        for row in report["warnings"]
        if row["code"] == "ROW_FILTER_COVERAGE"
    }
    region = coverage["policy.rj.region"]
    assert region["filtered"] == ["entity.rj_store"]
    assert region["covered"] == ["entity.rj_line", "entity.rj_order"]
    assert "entity.rj_customer" in region["uncovered"]


# Pass units: the structural verifier and render-ordered slots.

ROW = RowFilter("p", "t", "c", ParameterSlot("customer_id", "string"), entity="entity.t")
OTHER = RowFilter("q", "u", "d", ParameterSlot("store_id", "string"), entity="entity.u")


def _vis(*filters, modeled=("t", "u", "other"), **kwargs):
    from semantic_rails.row_filter_visibility import Visibility

    return Visibility(tuple(filters), modeled=frozenset(modeled), **kwargs)


def _scan(source, joins=(), where=(), ctes=()):
    return SqlSelect(
        [SqlField(SqlIdentifier(["v"]), "v")], source, list(joins), list(where), ctes=list(ctes)
    )


def test_every_occurrence_gets_its_predicate_where_it_enters_its_scope():
    on = SqlBinary(SqlIdentifier(["other", "k"]), "=", SqlIdentifier(["t", "k"]))
    statement = _scan(
        SqlTableRef("other"),
        joins=[SqlJoin("LEFT", SqlTableRef("t"), on), SqlJoin("INNER", SqlTableRef("u"), on)],
    )
    vis = _vis(ROW, OTHER)
    enforced = enforce_row_filters(statement, vis)
    verify_row_filters(enforced, vis)
    sql, slots = render_with_slots(enforced)
    assert "LEFT JOIN t ON t.c = ? AND other.k = t.k" in sql
    assert "WHERE\n  u.d = ?" in sql
    assert slots == (ROW.slot, OTHER.slot)


@pytest.mark.parametrize("join_type", ["RIGHT", "FULL"])
def test_right_or_full_joins_are_refused_in_a_restricted_scope(join_type):
    statement = _scan(
        SqlTableRef("t"), joins=[SqlJoin(join_type, SqlTableRef("other"), SqlLiteral(True))]
    )
    vis = _vis(ROW)
    assert _denied(lambda: enforce_row_filters(statement, vis)) == "row_filter_unsupported_query"


def test_the_verifier_catches_a_predicate_dropped_from_one_left_occurrence():
    on = SqlBinary(SqlIdentifier(["other", "k"]), "=", SqlIdentifier(["t", "k"]))
    statement = _scan(SqlTableRef("other"), joins=[SqlJoin("LEFT", SqlTableRef("t"), on)])
    vis = _vis(ROW)
    enforced = enforce_row_filters(statement, vis)
    dropped = replace(enforced, joins=[replace(enforced.joins[0], on=on)])
    assert _denied(lambda: verify_row_filters(dropped, vis)) == "row_filter_unsupported_query"
    # A predicate moved from the ON to the WHERE is a different answer, so it is refused too.
    moved = replace(dropped, where=[vis.predicate("t", ["t"])])
    assert _denied(lambda: verify_row_filters(moved, vis)) == "row_filter_unsupported_query"


@pytest.mark.parametrize(
    "statement",
    [
        _scan(SqlTableFunction("UNNEST", alias="x")),
        _scan(SqlTableRef("unmodeled")),
        _scan(SqlTableRef("main.t")),  # a second spelling of a restricted relation
        _scan(SqlTableRef("t"), ctes=[SqlCte("t", _scan(SqlTableRef("other")))]),  # shadowing
        _scan(
            SqlTableRef("a"),
            ctes=[SqlCte("a", _scan(SqlTableRef("other"))), SqlCte("a", _scan(SqlTableRef("t")))],
        ),  # duplicate CTE names
    ],
    ids=["table_function", "unmodeled", "two_spellings", "shadowing_cte", "duplicate_cte"],
)
def test_reads_the_pass_cannot_prove_are_refused(statement):
    vis = _vis(ROW)
    assert _denied(lambda: verify_row_filters(enforce_row_filters(statement, vis), vis)) in {
        "row_filter_unsupported_query",
        "row_filter_coverage_unsupported",
    }


def test_a_forced_verifier_failure_surfaces_as_a_policy_refusal(runtime, monkeypatch):
    monkeypatch.setattr(row_filters_module, "_add_predicates", lambda select, *_: select)
    query = _query({"revenue": REVENUE}, ["dimension.rj_store_store_name"])
    assert _denied(lambda: runtime.query(query, context=WEST)) == "row_filter_unsupported_query"


def test_two_filters_bind_in_render_order_across_hoisted_ctes_exists_and_subqueries(package):
    """Two filters on different attributes of one type: slots come from the render itself, so
    a hoisted CTE, an EXISTS body, an IN subquery, an ON and a WHERE each bind their own."""
    config = Runtime.from_path(str(package)).package_config
    context = _context("West", roles=["segment_scoped"], segment="Retail").to_policy_context()
    filters = row_filters_for_context(config, context)
    vis = visibility_for(config, filters)
    count = SqlField(SqlIdentifier(["order_fact", "order_id"]), "v")
    inner = SqlSelect([count], SqlTableRef("order_fact"))

    def nested(name):  # its CTE is hoisted ahead of the enclosing statement by the renderer
        return SqlSelect(
            [SqlField(SqlIdentifier([name, "v"]), "v")],
            SqlTableRef(name),
            ctes=[SqlCte(name, inner)],
        )

    statement = SqlSelect(
        [SqlField(SqlIdentifier(["c", "customer_id"]), "customer_id")],
        SqlTableRef("customer_dim", "c"),
        joins=[
            SqlJoin(
                "LEFT",
                SqlTableRef("store_dim", "s"),
                SqlBinary(SqlIdentifier(["s", "store_id"]), "=", SqlLiteral("s1")),
            )
        ],
        where=[
            SqlExists(
                SqlSelect(
                    [count],
                    SqlTableRef("order_fact"),
                    where=[
                        SqlBinary(
                            SqlIdentifier(["order_fact", "customer_id"]),
                            "=",
                            SqlIdentifier(["c", "customer_id"]),
                        )
                    ],
                )
            ),
            SqlInQuery(
                SqlIdentifier(["c", "customer_id"]),
                SqlSelect(
                    [SqlField(SqlIdentifier(["order_fact", "customer_id"]), "v")],
                    SqlTableRef("order_fact"),
                    where=[SqlInQuery(SqlIdentifier(["order_fact", "order_id"]), nested("n1"))],
                ),
            ),
        ],
        ctes=[SqlCte("unused", nested("n2"))],
    )
    enforced = enforce_row_filters(statement, vis)
    verify_row_filters(enforced, vis)
    sql, slots = render_with_slots(enforced)
    values = {"region": "West", "segment": "Retail"}
    with duckdb.connect(str(package / "rj.duckdb"), read_only=True) as connection:
        got = connection.execute(sql, [values[slot.attribute] for slot in slots]).fetchall()
        swapped = connection.execute(
            sql, [values[slot.attribute] for slot in reversed(slots)]
        ).fetchall()
    assert sorted(got) == [("c1",)]  # Retail customers with a visible order
    assert sorted(swapped) != sorted(got) or len(set(slots)) == 1


def test_the_limit_probe_reads_the_visible_database(runtime):
    query = {
        **_query({"revenue": REVENUE}, ["dimension.rj_store_store_name"]),
        "order_by": [{"field": "revenue", "direction": "desc"}],
        "limit": 1,
    }

    def ties(result):
        return [row for row in result.get("warnings") or [] if row["code"] == "TIES_AT_LIMIT"]

    west = runtime.query(query, context=WEST)
    assert len(_rows(west)) == 1 and ties(west)  # Elm and Oak tie at 30
    everyone = runtime.query(query, context=EVERYONE)
    assert _rows(everyone) == [("Pine", 900)] and not ties(everyone)


def test_zero_rows_skip_the_data_coverage_probe_under_any_filter(runtime, monkeypatch):
    probes = []
    monkeypatch.setattr(
        runtime_module, "_data_coverage_probe", lambda *a, **k: probes.append(1) or {}
    )
    window = {
        "select": [{"expression": REVENUE, "as": "revenue"}],
        "group_by": ["dimension.rj_store_store_name"],
        "time": {"temporal_role": "temporal_role.rj_order_ordered_at", "grain": "month",
                 "start": "2027-01-01"},
    }  # fmt: skip
    runtime.query(window, context=_context("North"))
    assert probes == []


def test_compile_never_reads_values_and_unfiltered_sql_is_unchanged(runtime):
    query = _query({"revenue": REVENUE}, ["dimension.rj_store_store_name"])
    west = runtime.compile(query, context=WEST)["rendered_sql"]
    east = runtime.compile(query, context=EAST)["rendered_sql"]
    assert west == east and "West" not in west
    assert "IN (" in west
    internal = runtime.compile(query, context=EVERYONE)["rendered_sql"]
    assert "?" not in internal


def test_compile_query_reports_slots_from_the_render(package):
    runtime = Runtime.from_path(str(package))
    filters = row_filters_for_context(
        runtime.package_config,
        _context("West", roles=["segment_scoped"], segment="Pro").to_policy_context(),
    )
    query = _query({"revenue": REVENUE}, ["dimension.rj_store_store_name"])
    compiled = compile_query(runtime._config, runtime.registry, query, row_filters=filters)
    prepared = compiled["prepared_query"]
    assert prepared.sql.count("?") == len(prepared.parameters) > 2
    assert {slot.attribute for slot in prepared.parameters} == {"region", "segment"}


def _order_column(column):
    return {"kind": "column", "entity": "entity.rj_order", "column": column}


def _literal(value):
    return {"kind": "literal", "value": value}


def _call(name, *args):
    return {"kind": "call", "name": name, "args": list(args)}


@pytest.mark.parametrize(
    ("call", "refused"),
    [
        (_call("CAST", _order_column("order_id"), _literal("INTEGER")), True),
        (_call("LN", _order_column("amount")), True),
        (_call("ABS", _order_column("amount")), True),  # overflows on the least integer
        (_call("JSON_EXTRACT", _order_column("order_id"), _literal("$.a")), True),
        (_call("SUBSTRING", _order_column("order_id"), _literal(1), _literal(1)), True),
        (_call("CAST", _order_column("amount"), _literal("VARCHAR")), False),
        (_call("COALESCE", _order_column("order_id"), _literal("x")), False),
        (_call("LOWER", _order_column("order_id")), False),
        (_call("ROUND", _order_column("amount")), False),
    ],
    ids=["cast_to_integer", "ln", "abs", "json_extract", "substring", "cast_to_text",
         "coalesce", "lower", "round"],
)  # fmt: skip
def test_a_row_level_call_that_can_fail_on_a_value_is_refused(runtime, call, refused):
    """A hidden order whose id isn't a number would make CAST(order_id AS INTEGER) fail,
    telling a caller something about a row they can't see: under a filter only calls that
    can't fail on any value are read, whoever wrote them."""
    condition = {"kind": "comparison", "op": "IS NOT", "left": call, "right": _literal(None)}
    aggregate = {"kind": "aggregate_if", "aggregation": "sum", "condition": condition,
                 "value": _order_column("amount")}  # fmt: skip
    query = _query({"n": aggregate})
    if refused:
        reason = _denied(lambda: runtime.compile(query, context=WEST))
        assert reason == "row_filter_fallible_expression"
        assert runtime.compile(query, context=EVERYONE)["ok"] is True
    else:
        assert runtime.compile(query, context=WEST)["ok"] is True


def test_a_restricted_models_declared_values_are_read_live_under_the_filter(runtime):
    """The store picker lists only the caller's stores, read live under their filter, even
    though the author typed every store; a model no filter reaches keeps its declared list."""
    adapter = SemanticLayerMCPAdapter(runtime)

    def values(context, dimension, live=True):
        arguments = {"dimension_id": dimension, "allow_live_query": live}
        if live:
            arguments["query"] = {"select": [{"expression": REVENUE, "as": "revenue"}]}
        result = adapter.call_tool("valid-values", arguments, request_context=context)
        return sorted(str(row["value"]) for row in result.get("values") or [])

    # Without a live read, the declared list is withheld from the filtered caller.
    assert values(EVERYONE, "dimension.rj_store_store_name", live=False) == ["Elm", "Oak", "Pine"]
    assert values(WEST, "dimension.rj_store_store_name", live=False) == []
    assert values(WEST, "dimension.rj_customer_segment", live=False) == ["Pro", "Retail"]

    assert values(EVERYONE, "dimension.rj_store_store_name") == ["Elm", "Oak", "Pine"]
    assert values(WEST, "dimension.rj_store_store_name") == ["Elm", "Oak"]
    assert values(WEST, "dimension.rj_customer_segment") == ["Pro", "Retail"]
    card = adapter.call_tool(
        "inspect", {"object_id": "dimension.rj_store_store_name", "verbosity": "full"},
        request_context=WEST,
    )  # fmt: skip
    assert "Pine" not in str(card)


@pytest.fixture
def home_stores(tmp_path):
    """Customers have a home store too (c1 Pine, c2 Elm, c3 Pine), so the region filter
    reaches customers through their home store, which isn't how an order is owned."""
    root = _package(tmp_path / "rj_home")
    graph = yaml.safe_load((root / "graph.yml").read_text())
    graph["graph"]["relationships"]["customers_home_store"] = {
        "id": "relationship.customers_home_store",
        "entities": ["customer", "store"],
        "cardinality": "many_to_one",
    }
    _put(root, "graph.yml", graph)
    model = yaml.safe_load((root / "models/customers.yml").read_text())
    model["model"]["entities"]["store"] = {}
    _put(root, "models/customers.yml", model)
    with duckdb.connect(str(root / "rj.duckdb")) as connection:
        connection.execute(SEED)
        connection.execute("ALTER TABLE customer_dim ADD COLUMN store_id VARCHAR")
        connection.execute(
            "UPDATE customer_dim SET store_id = CASE customer_id WHEN 'c2' THEN 's1' ELSE 's3' END"
        )
    return Runtime.from_path(str(root))


def test_a_lookup_that_is_not_the_owners_route_is_held(home_stores, monkeypatch):
    by_segment = _query({"revenue": REVENUE}, ["dimension.rj_customer_segment"])
    assert _denied(lambda: home_stores.query(by_segment, context=WEST)) == (
        "row_filter_route_not_owned"
    )
    # Held for what it would disclose, not for safety: without the hold, the visible orders
    # of customers homed in a hidden store land in the unknown group.
    from semantic_rails.compiler_parts import binding

    monkeypatch.setattr(binding, "hold_unowned_reads", lambda *a, **k: None)
    assert sorted(_rows(home_stores.query(by_segment, context=WEST)), key=str) == [
        ("Pro", 20),
        (None, 40),
    ]


def test_an_ambiguous_route_is_offered_only_if_every_option_answers(home_stores):
    by_store = _query({"qty": QTY}, ["dimension.rj_store_store_name"])
    with pytest.raises(SemanticLayerError) as asked:
        home_stores.compile(by_store, context=EVERYONE)
    assert asked.value.code == "AMBIGUOUS_PATH"
    with pytest.raises(SemanticLayerError) as held:
        home_stores.compile(by_store, context=WEST)
    assert held.value.code == "POLICY_DENIED" and "clarification" not in held.value.details


def test_a_shared_model_is_read_whole_by_every_filtered_caller(tmp_path):
    root = _package(tmp_path / "rj", [{**REGION, "shared": ["entity.rj_customer"]}])
    with duckdb.connect(str(root / "rj.duckdb")) as connection:
        connection.execute(SEED)
    runtime = Runtime.from_path(str(root))
    customers = {"kind": "aggregate", "measure": "measure.rj.customer_count",
                 "aggregation": "count_distinct"}  # fmt: skip
    assert _rows(runtime.query(_query({"n": customers}), context=WEST)) == [(3,)]
    # Shared only loosens what the filter doesn't reach: orders stay the caller's own.
    assert _rows(runtime.query(_query({"revenue": REVENUE}), context=WEST)) == [(60,)]
    report, _ = parse_config_report(resolve_package_reference(path=str(root)))
    (coverage,) = [
        row["details"] for row in report["warnings"] if row["code"] == "ROW_FILTER_COVERAGE"
    ] or [None]
    assert coverage is None or "entity.rj_customer" not in coverage["uncovered"]


def test_a_warehouse_without_subqueries_in_on_refuses_an_optional_covered_lookup():
    owner = ParentHop("k", "u", "k")
    on = SqlBinary(SqlIdentifier(["other", "k"]), "=", SqlIdentifier(["t", "k"]))
    left = _scan(SqlTableRef("other"), joins=[SqlJoin("LEFT", SqlTableRef("t"), on)])
    covered = _vis(OTHER, parents={"t": (owner,)}, subquery_in_join_on=False)
    assert _denied(lambda: enforce_row_filters(left, covered)) == (
        "row_filter_shape_unsupported_by_warehouse"
    )
    # A directly filtered relation needs no subquery, and an INNER read puts it in WHERE.
    direct = _vis(ROW, subquery_in_join_on=False)
    verify_row_filters(enforce_row_filters(left, direct), direct)
    inner = replace(left, joins=[SqlJoin("INNER", SqlTableRef("t"), on)])
    verify_row_filters(enforce_row_filters(inner, covered), covered)


@pytest.mark.parametrize(
    "model", ["entity.rj_store", "entity.rj_order"], ids=["filtered", "covered"]
)
def test_shared_naming_a_model_the_filter_reaches_is_invalid_config(tmp_path, model):
    root = _package(tmp_path / "rj", [{**REGION, "shared": [model]}])
    with pytest.raises(SemanticLayerError) as caught:
        Runtime.from_path(str(root))
    assert caught.value.code == "INVALID_CONFIG" and model in str(caught.value)
    report, _ = parse_config_report(resolve_package_reference(path=str(root)))
    assert [row["code"] for row in report["errors"]] == ["INVALID_CONFIG"]
    assert "shared" in report["errors"][0]["message"]


def _lines_order(config, **changes):
    return [
        replace(row, **changes) if row.id == "relationship.lines_order" else row
        for row in config.relationships
    ]


def _another_order_model(table):
    from semantic_rails.schema import EntityConfig

    return EntityConfig(id="entity.rj_order_copy", table=table, key_columns=["order_id"])


@pytest.mark.parametrize(
    ("change", "model", "measure"),
    [
        (
            lambda c: {"relationships": _lines_order(
                c, source_columns=["order_id", "line_id"], target_columns=["order_id", "order_id"]
            )},
            "entity.rj_line",
            QTY,
        ),
        (
            lambda c: {"relationships": _lines_order(c, temporal_validity={"start": "s", "end": "e"})},
            "entity.rj_line",
            QTY,
        ),
        (
            lambda c: {"entities": [*c.entities, _another_order_model("order_fact")]},
            "entity.rj_order",
            REVENUE,
        ),
        (
            lambda c: {"entities": [*c.entities, _another_order_model("main.order_fact")]},
            "entity.rj_order",
            REVENUE,
        ),
    ],
    ids=["composite_key", "temporal_validity_hop", "models_disagree", "two_spellings"],
)  # fmt: skip
def test_a_model_the_filter_cant_be_traced_through_is_refused_never_read(
    package, change, model, measure
):
    """Refused under the filter, never read as if no filter reached it; validate says why."""
    from semantic_rails.row_filter_visibility import coverage_report

    base = Runtime.from_path(str(package)).package_config
    config = replace(base, **change(base))
    runtime = Runtime.from_config(config, source_path=str(package))
    try:
        reason = _denied(lambda: runtime.query(_query({"v": measure}), context=WEST))
    finally:
        runtime.close()
    assert reason == "row_filter_coverage_unsupported"
    (region,) = [row for row in coverage_report(config) if row["policy_id"] == REGION["id"]]
    assert model in region["unsupported"] and model not in region["covered"]


def test_an_undecided_ownership_route_is_refused_and_listed(home_stores):
    """Lines reach stores through their order or through their order's customer's home
    store: which owns them is undecided, so lines are refused under the filter."""
    from semantic_rails.row_filter_visibility import coverage_report

    assert _denied(lambda: home_stores.query(_query({"qty": QTY}), context=WEST)) == (
        "row_filter_coverage_unsupported"
    )
    (region,) = [
        row
        for row in coverage_report(home_stores.package_config)
        if row["policy_id"] == REGION["id"]
    ]
    assert "entity.rj_line" in region["unsupported"]


@pytest.mark.parametrize("hidden", [frozenset(), frozenset({"entity.rj_customer"})])
def test_a_hold_names_nothing_the_callers_view_hides(runtime, home_stores, hidden):
    """A hold names the models and relationships it read only when the caller's view shows
    them; otherwise it says only what happened."""
    from semantic_rails.row_filter_visibility import hold_unowned_reads
    from semantic_rails.visible_view import build_view

    route = ["relationship.orders_customer"]

    def hold(base, **reads):
        filters = row_filters_for_context(base, WEST.to_policy_context())
        with pytest.raises(SemanticLayerError) as caught:
            hold_unowned_reads(build_view(base, hidden), visibility_for(base, filters), **reads)
        return caught.value

    not_owned = hold(
        home_stores.package_config,
        roots=(),
        routes=[("entity.rj_order", "entity.rj_customer", route)],
    )
    uncovered = hold(runtime.package_config, roots=["entity.rj_customer"], routes=[])
    for error, reason in (
        (not_owned, "row_filter_route_not_owned"),
        (uncovered, "row_filter_uncovered_root"),
    ):
        assert (error.code, error.details["reason"]) == ("POLICY_DENIED", reason)
        if hidden:
            assert "objects" not in error.details
            assert "rj_customer" not in f"{error} {error.details}"
        else:
            assert "entity.rj_customer" in error.details["objects"]


DOUBLED = {"kind": "aggregate", "measure": "measure.rj.doubled", "aggregation": "sum"}


@pytest.fixture
def overflow(tmp_path):
    """Package row arithmetic (an order's ``big`` doubled) that overflows a 64-bit integer only
    on an East order."""
    root = _package(tmp_path / "rj_overflow")
    model = yaml.safe_load((root / "models/orders.yml").read_text())
    model["model"]["measures"]["doubled"] = {
        "kind": "aggregate",
        "rollup": "additive",
        "expr": {"kind": "arithmetic", "op": "multiply",
                 "left": {"kind": "column", "column": "big"},
                 "right": {"kind": "literal", "value": 2}},
    }  # fmt: skip
    _put(root, "models/orders.yml", model)
    with duckdb.connect(str(root / "rj.duckdb")) as connection:
        connection.execute(SEED)
        connection.execute("ALTER TABLE order_fact ADD COLUMN big BIGINT DEFAULT 1")
        connection.execute("UPDATE order_fact SET big = 9000000000000000000 WHERE order_id = 'o4'")
    return root


def test_package_arithmetic_that_overflows_only_on_a_hidden_row_never_moves_an_answer(overflow):
    """DuckDB evaluates the measure's arithmetic only on rows that passed the visibility
    predicate in the same scope: the West answer equals the answer on the West's visible
    rows, while an unfiltered caller (who can see the row) gets the warehouse's error."""
    runtime = Runtime.from_path(str(overflow))
    query = _query({"v": DOUBLED})
    try:
        west = _rows(runtime.query(query, context=WEST))
        with pytest.raises(SemanticLayerError) as unfiltered:
            runtime.query(query, context=EVERYONE)
    finally:
        runtime.close()
    assert unfiltered.value.code == "QUERY_EXECUTION_ERROR"
    with duckdb.connect(str(overflow / "rj.duckdb")) as connection:
        connection.execute(
            "DELETE FROM order_fact WHERE store_id IS NULL OR store_id NOT IN "
            "(SELECT store_id FROM store_dim WHERE region = 'West')"
        )
    visible_only = Runtime.from_path(str(overflow))
    try:
        assert west == _rows(visible_only.query(query, context=EVERYONE)) == [(6,)]
    finally:
        visible_only.close()


@pytest.mark.parametrize("warehouse", ["duckdb", "postgres", "snowflake"])
def test_package_arithmetic_reads_rows_in_the_scope_that_checks_their_visibility(
    overflow, warehouse
):
    """On every warehouse that runs row filters, a measure's row arithmetic is in the same
    SELECT as its rows' visibility predicate, never in a scope that reads unchecked rows.
    Whether a warehouse evaluates it before that predicate within the scope is the documented
    residual (integer overflow on a hidden row)."""
    base = Runtime.from_path(str(overflow)).package_config
    config = replace(base, package=replace(base.package, warehouse=warehouse))
    filters = row_filters_for_context(config, WEST.to_policy_context())
    compiled = compile_query(config, None, _query({"v": DOUBLED}), row_filters=filters)
    from semantic_rails.sql_ast import sql_nodes

    scopes = [
        node
        for node in sql_nodes(compiled["sql_ast"])
        if isinstance(node, SqlSelect)
        and any(isinstance(item, SqlBinary) and item.op == "*" for item in sql_nodes(node.select))
    ]
    assert scopes
    for scope in scopes:
        assert scope.from_table.name == "order_fact"
        assert any(isinstance(item, SqlInQuery) for item in sql_nodes(scope.where))
