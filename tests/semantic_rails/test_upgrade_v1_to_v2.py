"""The translator from the previous query call shape (v1) to the current one (v2)."""

from __future__ import annotations

import copy
from collections import Counter
from pathlib import Path

import pytest

from semantic_rails.query_call import read_call
from semantic_rails.runtime import Runtime
from semantic_rails.upgrade.v1_to_v2 import (
    FLAG_CODES,
    rename_output_key,
    translate_columns,
    translate_query,
    translate_rows,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = "2017-09-04T12:00:00Z"
ROLE = "temporal_role.shop_order_ordered_at"
REV = "measure.shop.revenue"
# Ids of the bundled jaffle_shop package, so each translated call can be validated.
REVENUE = "measure.jaffle.revenue_usd"
ORDERS = "measure.jaffle.order_count"
STORE = "dimension.jaffle_store_name"
ORDER_TIME = "temporal_role.jaffle_order_time"


def codes(result) -> list[str]:
    return [flag["code"] for flag in result.flags]


def tq(query):
    """Translate, and check the input is untouched and a second pass changes nothing."""
    before = copy.deepcopy(query)
    result = translate_query(query)
    assert query == before, "the translator mutated its input"
    again = translate_query(result.query)
    assert again.query == result.query, "a second pass changed the query"
    assert again.call == {}, "a second pass moved more keys out"
    assert not (Counter(codes(again)) - Counter(codes(result))), "a second pass raised new flags"
    for flag in result.flags:
        assert set(flag) == {"code", "path", "detail"} and flag["code"] in FLAG_CODES
    return result


@pytest.fixture(scope="module")
def jaffle():
    runtime = Runtime.from_path(ROOT / "configs/semantic_rails/jaffle_shop")
    yield runtime
    runtime.close()


def validates(runtime: Runtime, query: dict) -> None:
    report = runtime.validate(query, now=NOW)
    assert report["ok"], report.get("errors")


# --- Top-level fields --------------------------------------------------------------------


def test_version_and_explain_are_dropped():
    r = tq({"version": 1, "explain": False, "select": [{"expression": {"measure": REV}}]})
    assert (r.query, r.flags) == ({"select": [REV]}, [])
    assert codes(tq({"explain": True, "select": [REV]})) == ["EXPLAIN_DROPPED"]


def test_call_options_leave_the_body():
    query = {
        "select": [REV],
        "debug": True,
        "verbosity": "full",
        "sql_profile": "pretty",
        "limits": {"max_rows": 5},
        "request_id": "abc",
    }
    r = tq(query)
    assert r.query == {"select": [REV]}
    assert r.call == {
        "verbosity": "full",
        "sql_profile": "pretty",
        "limits": {"max_rows": 5},
        "request_id": "abc",
        "debug": True,
    }


def test_nested_transport_keys_leave_the_body(jaffle):
    v1 = {
        "select": [{"expression": {"measure": REVENUE}}],
        "max_rows": 100000,
        "row_format": "columns",
        "mode": "run",
    }
    r = tq(v1)
    assert r.query == {"select": [REVENUE]}
    assert r.call == {"max_rows": 100000, "row_format": "columns", "mode": "run"}
    assert r.flags == []
    validates(jaffle, r.query)


def test_policy_context_moves_to_now_and_context():
    r = tq({"select": [REV], "policy_context": {"now": "2024-07-05", "audience": "finance"}})
    assert r.query == {"select": [REV]}
    assert r.call == {"now": "2024-07-05", "context": {"audience": "finance"}}
    assert tq({"select": [REV], "policy_context": {"now": ""}}).call == {}


def test_kept_top_level_keys_are_flagged():
    query = {"select": [REV], "temporal_role_overrides": {REV: "temporal_role.x"}}
    r = tq(query)
    assert (r.query, codes(r)) == (query, ["NEEDS_PACKAGE_METRIC"])
    assert codes(tq({"select": [REV], "export": True})) == ["EXPORT_RETIRED"]
    assert codes(tq({"select": [REV], "limit": 0})) == ["LIMIT_ZERO"]
    r = tq({"select": [REV], "_note": "kept", "filters": []})
    assert (r.query, codes(r)) == (
        {"select": [REV], "_note": "kept", "filters": []},
        ["UNKNOWN_KEY"],
    )


# --- Routes ------------------------------------------------------------------------------

ROUTE = {
    "source_entity": "entity.jaffle_customer",
    "target_entity": "entity.jaffle_order",
    "relationship_path": ["relationship.orders_customer"],
}
ROUTE_CALL = {
    "from": "entity.jaffle_customer",
    "to": "entity.jaffle_order",
    "via": ["relationship.orders_customer"],
}
# Customers by an order dimension: two routes reach the order, so the call needs one.
WALKS_CUSTOMER_TO_ORDER = {
    "select": [{"measure": "measure.jaffle.customer_count"}],
    "group_by": ["dimension.jaffle_order_is_large_order"],
}
WALKED = {
    "select": ["measure.jaffle.customer_count"],
    "group_by": ["dimension.jaffle_order_is_large_order"],
}


def test_route_decisions_become_routes(jaffle):
    v1 = {
        **WALKS_CUSTOMER_TO_ORDER,
        "route_decisions": [{**ROUTE, "label": "any of the Customer's Orders"}],
    }
    r = tq(v1)
    assert r.query == {**WALKED, "routes": [ROUTE_CALL]}
    assert r.flags == []
    validates(jaffle, r.query)


def test_a_route_option_object_becomes_its_decision(jaffle):
    option = {
        "id": "customer_order",
        "meaning": "any of the Customer's Orders",
        "relationship_path": ["relationship.orders_customer"],
        "decision": {**ROUTE, "label": "any of the Customer's Orders"},
    }
    r = tq({**WALKS_CUSTOMER_TO_ORDER, "route_decisions": [option]})
    assert r.query == {**WALKED, "routes": [ROUTE_CALL]}
    validates(jaffle, r.query)
    clashing = {**option, "relationship_path": ["relationship.other"]}
    query = {"select": [REV], "route_decisions": [clashing]}
    r = tq(query)
    assert (r.query, codes(r)) == (query, ["INVALID_V1_SHAPE"])


def test_a_route_option_id_is_kept():
    query = {"select": [REV], "route_decisions": ["customer_order"]}
    r = tq(query)
    assert (r.query, codes(r)) == (query, ["ROUTE_OPTION_ID"])
    assert r.flags[0]["path"] == "route_decisions[0]"


# --- Select ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item",
    [
        {"expression": {"kind": "metric", "metric": "metric.sales.aov"}},
        {"expression": {"metric": "metric.sales.aov"}, "as": "aov"},
        {"metric": "metric.sales.aov"},
    ],
)
def test_metric_refs_become_strings(item):
    assert tq({"select": [item]}).query == {"select": ["metric.sales.aov"]}


@pytest.mark.parametrize(
    "item",
    [
        {"expression": {"kind": "measure", "measure": REV}},
        {"expression": {"kind": "measure_ref", "measure": REV}},
        {"expression": {"measure": REV, "aggregation": "", "parameters": {}}},
        {"measure": REV},
    ],
)
def test_measure_refs_without_overrides_become_strings(item):
    assert tq({"select": [item]}).query == {"select": [REV]}


def test_an_alias_and_an_override_are_kept():
    r = tq({"select": [{"expression": {"metric": "metric.sales.aov_usd"}, "as": "aov"}]})
    assert r.query == {"select": [{"expression": "metric.sales.aov_usd", "as": "aov"}]}
    r = tq({"select": [{"measure": "measure.shop.amount", "aggregation": "median"}]})
    assert r.query == {
        "select": [
            {
                "expression": {
                    "kind": "measure",
                    "measure": "measure.shop.amount",
                    "aggregation": "median",
                },
                "as": "amount",
            }
        ]
    }


def test_select_dimensions_lift_into_group_by():
    query = {"select": [{"dimension": "dimension.store"}, {"measure": REV}]}
    assert tq(query).query == {"select": [REV], "group_by": ["dimension.store"]}
    query = {"select": [{"dimension": "dimension.city"}, {"measure": REV}], "group_by": ["d.x"]}
    r = tq(query)
    assert codes(r) == ["SELECT_DIMENSION_REFUSED"]


def test_kinds_keep_their_shape_with_string_operands():
    v1 = {
        "select": [
            {
                "as": "net",
                "expression": {
                    "kind": "binary",
                    "op": "SUBTRACT",
                    "left": {"measure": REV},
                    "right": {
                        "kind": "ratio",
                        "numerator": {"metric": "metric.a"},
                        "denominator": {"measure": "measure.b", "aggregation": "max"},
                    },
                },
            },
            {"expression": {"kind": "rolling", "input": {"metric": "metric.a"}, "window": 7}},
        ]
    }
    assert tq(v1).query == {
        "select": [
            {
                "expression": {
                    "kind": "arithmetic",
                    "op": "subtract",
                    "left": REV,
                    "right": {
                        "kind": "ratio",
                        "numerator": "metric.a",
                        "denominator": {
                            "kind": "measure",
                            "measure": "measure.b",
                            "aggregation": "max",
                        },
                    },
                },
                "as": "net",
            },
            # The previous shape named an unaliased kind expr_<n>; the alias is now written.
            {"expression": {"kind": "rolling", "input": "metric.a", "window": 7}, "as": "expr_2"},
        ]
    }


@pytest.mark.parametrize(
    ("op", "word"), [("div", "divide"), ("sub", "subtract"), ("mul", "multiply")]
)
def test_arithmetic_op_aliases_become_words(jaffle, op, word):
    v1 = {
        "select": [
            {
                "as": "x",
                "expression": {
                    "kind": "arithmetic",
                    "op": op,
                    "left": {"measure": REVENUE},
                    "right": {"measure": ORDERS},
                },
            }
        ]
    }
    r = tq(v1)
    expression = {"kind": "arithmetic", "op": word, "left": REVENUE, "right": ORDERS}
    assert (r.query, r.flags) == ({"select": [{"expression": expression, "as": "x"}]}, [])
    validates(jaffle, r.query)


@pytest.mark.parametrize("key", ["operands", "terms"])
def test_operand_lists_fold_left(jaffle, key):
    v1 = {
        "select": [
            {
                "as": "x",
                "expression": {
                    "kind": "arithmetic",
                    "op": "sub",
                    key: [
                        {"measure": REVENUE},
                        {"measure": ORDERS},
                        {"kind": "literal", "value": 1},
                    ],
                },
            }
        ]
    }
    r = tq(v1)
    inner = {"kind": "arithmetic", "op": "subtract", "left": REVENUE, "right": ORDERS}
    expression = {
        "kind": "arithmetic",
        "op": "subtract",
        "left": inner,
        "right": {"kind": "literal", "value": 1},
    }
    assert (r.query, r.flags) == ({"select": [{"expression": expression, "as": "x"}]}, [])
    validates(jaffle, r.query)


@pytest.mark.parametrize(
    "expression",
    [
        {"kind": "arithmetic", "op": "add", "operands": [REV]},
        {"kind": "arithmetic", "op": "add", "operands": [REV, REV], "left": REV},
        {"kind": "arithmetic", "op": "add", "operands": [REV, REV], "terms": [REV, REV]},
    ],
)
def test_operand_lists_the_previous_engine_refused_are_kept(expression):
    query = {"select": [{"as": "x", "expression": expression}]}
    r = tq(query)
    assert (r.query, codes(r)) == (query, ["INVALID_V1_SHAPE"])


def test_comparison_op_aliases_become_symbols(jaffle):
    condition = {
        "kind": "comparison",
        "op": "gte",
        "left": {"kind": "column", "column": "order_total", "entity": "entity.jaffle_order"},
        "right": {"kind": "literal", "value": 10},
    }
    v1 = {
        "select": [
            {
                "as": "n",
                "expression": {
                    "kind": "aggregate_if",
                    "aggregation": "count",
                    "condition": condition,
                },
            }
        ]
    }
    r = tq(v1)
    assert r.query["select"][0]["expression"]["condition"]["op"] == ">="
    validates(jaffle, r.query)


def test_prior_period_shorthand():
    v1 = {
        "select": [
            {
                "as": "prev",
                "expression": {
                    "kind": "prior_period",
                    "measure": REV,
                    "offset": -1,
                    "grain": "month",
                },
            }
        ]
    }
    assert tq(v1).query == {
        "select": [
            {
                "expression": {
                    "kind": "prior_period",
                    "input": REV,
                    "offset": {"unit": "month", "value": 1},
                },
                "as": "prev",
            }
        ]
    }


def test_retired_expression_spellings_are_rewritten():
    v1 = {
        "select": [
            {
                "as": "c",
                "expression": {
                    "kind": "conversion",
                    "base": {"measure": "measure.a"},
                    "converted": {"metric": "metric.b"},
                    "matching": "first_converted_after_base",
                    "null_behavior": "zero",
                },
            }
        ],
        "where": [{"kind": "in", "left": {"kind": "column", "column": "c"}, "values": [1]}],
        "path_policy": {"max_hops": 2},
    }
    r = tq(v1)
    assert r.query == {
        "select": [
            {
                "expression": {
                    "kind": "conversion",
                    "base": "measure.a",
                    "converted": "metric.b",
                    "matching_mode": "first_converted_after_base",
                },
                "as": "c",
            }
        ],
        "where": [{"kind": "in", "expr": {"kind": "column", "column": "c"}, "values": [1]}],
    }


# --- Predicates and filters --------------------------------------------------------------


def _scoped(predicate):
    return {
        "select": [
            {
                "as": "s",
                "expression": {
                    "kind": "scoped_aggregate",
                    "measure": REV,
                    "predicates": [predicate],
                },
            }
        ]
    }


def test_scoped_predicates_map_scope_to_over():
    base = {"input": {"measure": "measure.s"}, "entity": "entity.c", "op": ">", "value": 0}
    r = tq(_scoped({**base, "scope_mode": "entity_only"}))
    assert r.query["select"][0]["expression"]["predicates"] == [
        {"input": "measure.s", "entity": "entity.c", "op": "gt", "value": 0, "over": "all_time"}
    ]
    r = tq(
        _scoped({**base, "scope_mode": "entity_only", "time_alignment": "rolling_window_in_period"})
    )
    assert codes(r) == ["ALIGNMENT_COLLAPSED"]
    r = tq(_scoped(base))
    assert (r.query, codes(r)) == (_scoped(base), ["CONTEXTUAL_NO_PERIOD"])


def where_of(op, value="a"):
    r = tq({"select": [REV], "where": [{"field": "dimension.x", "op": op, "value": value}]})
    return r.query["where"][0], codes(r)


@pytest.mark.parametrize(
    ("op", "value", "expected", "flags"),
    [
        ("=", "a", ("is", "a"), []),
        ("equals", "a", ("is", "a"), []),
        ("IN", ["a", "b"], ("is", ["a", "b"]), []),
        ("!=", "a", ("not", "a"), ["NOT_KEEPS_UNKNOWNS"]),
        ("NOT IN", ["a"], ("not", ["a"]), ["NOT_KEEPS_UNKNOWNS"]),
        (">=", 3, ("gte", 3), []),
        ("IS NULL", None, ("missing", True), []),
        ("is_not_null", None, ("missing", False), []),
        ("!=", None, ("missing", False), []),
        ("LIKE", "%shop%", ("contains", "shop"), ["CONTAINS_CASE_INSENSITIVE"]),
        ("like", "shop%", ("starts_with", "shop"), ["CONTAINS_CASE_INSENSITIVE"]),
        ("LIKE", "%shop", ("ends_with", "shop"), ["CONTAINS_CASE_INSENSITIVE"]),
        (
            "NOT LIKE",
            "%shop%",
            ("not_contains", "shop"),
            ["CONTAINS_CASE_INSENSITIVE", "NOT_KEEPS_UNKNOWNS"],
        ),
        (
            "NOT LIKE",
            "shop%",
            ("not_starts_with", "shop"),
            ["CONTAINS_CASE_INSENSITIVE", "NOT_KEEPS_UNKNOWNS"],
        ),
    ],
)
def test_where_ops(op, value, expected, flags):
    item, got = where_of(op, value)
    assert (item["op"], item["value"]) == expected
    assert list(item) == ["field", "op", "value"]
    assert got == flags


@pytest.mark.parametrize(
    ("op", "value", "flag"),
    [
        ("LIKE", "%sh_p%", "UNTRANSLATABLE_OP"),
        ("LIKE", "a%b", "UNTRANSLATABLE_OP"),
        ("LIKE", "shop", "UNTRANSLATABLE_OP"),
        ("LIKE", "%Σ%", "UNTRANSLATABLE_OP"),
        ("ILIKE", "%a%", "UNTRANSLATABLE_OP"),
        ("IN", [], "EMPTY_LIST_VALUE"),
        ("IN", ["a", None], "NULL_IN_LIST"),
        ("=", ["a", "b"], "UNTRANSLATABLE_OP"),
        (">", None, "UNTRANSLATABLE_OP"),
        ("BETWEEN", [1, 2], "UNTRANSLATABLE_OP"),
    ],
)
def test_untranslatable_where_ops_are_kept(op, value, flag):
    item, got = where_of(op, value)
    assert item == {"field": "dimension.x", "op": op, "value": value}
    assert got == [flag]


def test_text_words_from_like_validate(jaffle):
    v1 = {
        "select": [{"expression": {"measure": ORDERS}}],
        "where": [
            {"field": STORE, "op": "LIKE", "value": "Brook%"},
            {"field": STORE, "op": "NOT LIKE", "value": "%delphia"},
        ],
    }
    r = tq(v1)
    assert r.query["where"] == [
        {"field": STORE, "op": "starts_with", "value": "Brook"},
        {"field": STORE, "op": "not_ends_with", "value": "delphia"},
    ]
    validates(jaffle, r.query)


def test_a_like_comparison_becomes_its_text_word(jaffle):
    def condition(op, pattern):
        return {
            "kind": "comparison",
            "op": op,
            "left": {"kind": "column", "column": "store_name", "entity": "entity.jaffle_store"},
            "right": {"kind": "literal", "value": pattern},
        }

    v1 = {
        "select": [
            {
                "as": "n",
                "expression": {
                    "kind": "aggregate_if",
                    "aggregation": "count",
                    "condition": condition("LIKE", "%brook%"),
                    "value": {
                        "kind": "column",
                        "column": "order_id",
                        "entity": "entity.jaffle_order",
                    },
                },
            }
        ]
    }
    r = tq(v1)
    assert r.query["select"][0]["expression"]["condition"] == condition("contains", "brook")
    assert codes(r) == ["CONTAINS_CASE_INSENSITIVE"]
    validates(jaffle, r.query)
    kept = {**v1, "select": [{**v1["select"][0]}]}
    kept["select"][0]["expression"] = {
        **v1["select"][0]["expression"],
        "condition": condition("LIKE", "b_o%"),
    }
    assert codes(tq(kept)) == ["UNTRANSLATABLE_OP"]


def test_child_groups_become_entity_filters():
    v1 = {
        "select": [REV],
        "where": [
            {
                "child": "entity.refund",
                "match": "none",
                "where": [{"field": "d.t", "op": "IN", "value": ["a"]}],
            }
        ],
    }
    assert tq(v1).query["where"] == [
        {"entity": "entity.refund", "none": [{"field": "d.t", "op": "is", "value": ["a"]}]}
    ]


def test_metric_filters_become_having_and_entity_filters():
    v1 = {
        "select": [{"expression": {"metric": "metric.aov"}, "as": "average_value"}],
        "metric_filters": [
            {"expression": {"kind": "metric", "metric": "metric.aov"}, "op": "<", "value": 5},
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.c",
                    "scope_mode": "entity_only",
                    "input": {"measure": "measure.n"},
                    "op": "=",
                    "value": 0,
                },
                "op": "=",
                "value": True,
            },
        ],
    }
    assert tq(v1).query == {
        "select": [{"expression": "metric.aov", "as": "average_value"}],
        "where": [
            {
                "entity": "entity.c",
                "having": [{"field": "measure.n", "op": "is", "value": 0}],
                "over": "all_time",
            }
        ],
        "having": [{"field": "metric.aov", "op": "lt", "value": 5}],
    }


def test_a_metric_filter_neither_ref_nor_selected_is_kept():
    item = {
        "expression": {
            "kind": "aggregate",
            "measure": REV,
            "filter": {"all": [{"field": "d.x", "op": ">=", "value": 1}]},
        },
        "op": ">",
        "value": 0,
    }
    r = tq({"select": [REV], "metric_filters": [item]})
    assert (r.query, codes(r)) == (
        {"select": [REV], "metric_filters": [item]},
        ["HAVING_NEEDS_SELECT"],
    )


# --- Time, order_by and output keys -------------------------------------------------------


def test_time_range_becomes_last_and_defaults_drop():
    v1 = {
        "select": [REV],
        "time": {
            "temporal_role": ROLE,
            "grain": "",
            "range": {"last": {"unit": "week", "value": 2}},
            "calendar_id": "default",
        },
    }
    r = tq(v1)
    assert (r.query["time"], codes(r)) == (
        {"temporal_role": ROLE, "last": {"unit": "week", "value": 2}},
        ["NAMED_WINDOW"],
    )
    r = tq(
        {
            "select": [REV],
            "time": {"temporal_role": ROLE, "grain": "quarter", "calendar_id": "fiscal"},
        }
    )
    assert codes(r) == ["CALENDAR_ID"]
    assert codes(tq({"select": [REV], "time": {"temporal_role": ROLE, "grain": "hour"}})) == [
        "GRAIN_RETIRED"
    ]


def test_order_by():
    v1 = {
        "select": [{"expression": {"measure": REV}, "as": "r"}],
        "time": {"temporal_role": ROLE, "grain": "month"},
        "order_by": [
            {"field": "time", "direction": "ASC"},
            {"field": f"{ROLE}__month", "direction": "Desc"},
        ],
    }
    assert tq(v1).query["order_by"] == [
        {"field": "period", "direction": "asc"},
        {"field": "period", "direction": "desc"},
    ]
    r = tq(
        {
            "select": [REV],
            "time": {"temporal_role": ROLE, "grain": "month"},
            "order_by": [{"field": f"{ROLE}__week"}],
        }
    )
    assert codes(r) == ["UNTRANSLATABLE_ORDER_BY"]


def test_output_keys():
    assert rename_output_key(f"{ROLE}__month") == "period"
    assert rename_output_key(f"{ROLE}__hour") == f"{ROLE}__hour"
    rows, flags = translate_rows([{f"{ROLE}__month": "2024-01-01", "revenue": 3}])
    assert (rows, flags) == ([{"period": "2024-01-01", "revenue": 3}], [])
    rows, flags = translate_rows([{ROLE: "2024-01-01T10:00:00"}])
    assert [flag["code"] for flag in flags] == ["ROLE_KEY_RAW"]
    assert translate_columns([f"{ROLE}__month", "revenue"]) == (["period", "revenue"], [])


# --- Whole calls -------------------------------------------------------------------------


def test_a_previous_call_translates_to_a_call_the_engine_reads(jaffle):
    v1 = {
        "version": 1,
        "select": [{"expression": {"kind": "measure", "measure": REVENUE}, "as": "revenue_usd"}],
        "group_by": [STORE],
        "where": [{"field": STORE, "op": "!=", "value": "Brooklyn"}],
        "time": {
            "temporal_role": ORDER_TIME,
            "grain": "month",
            "range": {"last": {"unit": "month", "value": 3}},
        },
        "order_by": [{"field": "time", "direction": "DESC"}],
        "policy_context": {"now": NOW},
    }
    r = tq(v1)
    assert r.query == {
        "select": [REVENUE],
        "group_by": [STORE],
        "where": [{"field": STORE, "op": "not", "value": "Brooklyn"}],
        "time": {
            "temporal_role": ORDER_TIME,
            "grain": "month",
            "last": {"unit": "month", "value": 3},
        },
        "order_by": [{"field": "period", "direction": "desc"}],
    }
    assert r.call == {"now": NOW}
    assert sorted(codes(r)) == ["NAMED_WINDOW", "NOT_KEEPS_UNKNOWNS"]
    validates(jaffle, r.query)


def test_a_current_call_passes_through_unchanged():
    v2 = {
        "select": [
            REVENUE,
            {"expression": {"kind": "share", "input": REVENUE, "over": [STORE]}, "as": "s"},
        ],
        "group_by": [STORE],
        "where": [{"field": STORE, "op": "starts_with", "value": "B"}],
    }
    r = tq(v2)
    assert (r.query, r.call, r.flags) == (v2, {}, [])
    read_call(r.query, now=NOW)
