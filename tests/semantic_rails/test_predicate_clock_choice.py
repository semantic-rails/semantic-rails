"""A bounded entity predicate needs an unambiguous clock for its window."""

from dataclasses import replace
from datetime import datetime

import duckdb
import pytest
import yaml

from semantic_rails.ast import normalize_query
from semantic_rails.compiler import (
    _compile_query_sql_ast,
    _object_default_query_temporal_role,
    _refuse_overridden_predicate_input,
    bind_metadata_objects,
    compile_query,
    lower_to_sql,
    plan_query,
)
from semantic_rails.compiler_parts import sql_lowering
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import AggregateExpr, parse_semantic_expression
from semantic_rails.request_context import RequestContext
from semantic_rails.resource_access import ResourceAccess
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

ORDERED = "temporal_role.clocks_order_ordered_at"
SHIPPED = "temporal_role.clocks_order_shipped_at"
SIGNED_UP = "temporal_role.clocks_customer_signed_up_at"


def test_conversion_predicate_window_filters_only_base_events_matches_reference(tmp_path):
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    config = load_package_config(str(package))
    example = yaml.safe_load((package / "examples/advanced.yml").read_text())["examples"][
        "signup_to_send_28d_for_high_order_rate_stores"
    ]
    with duckdb.connect(str(package / "jaffle_shop.duckdb"), read_only=True) as conn:
        expected = conn.execute(
            """
            WITH sessions AS (
                SELECT x.*, date_trunc('month', x.started_at) AS month, st.store_name
                FROM jaffle_storefront_session x JOIN jaffle_store st USING (store_id)
            ), predicate_rates AS (
                SELECT store_name, month,
                    COUNT(*) FILTER (WHERE EXISTS (
                        SELECT 1 FROM jaffle_order o JOIN jaffle_store os USING (store_id)
                        WHERE o.customer_id = s.customer_id
                          AND o.ordered_at >= s.started_at
                          AND o.ordered_at < s.started_at + INTERVAL 7 DAY
                          AND os.store_name = s.store_name
                    ))::DOUBLE / COUNT(*) AS rate
                FROM sessions s GROUP BY 1, 2
            ), output_rates AS (
                SELECT store_name, month,
                    COUNT(*) FILTER (WHERE EXISTS (
                        SELECT 1 FROM jaffle_order o
                        WHERE o.customer_id = s.customer_id
                          AND o.ordered_at >= s.started_at
                          AND o.ordered_at < s.started_at + INTERVAL 28 DAY
                    ))::DOUBLE / COUNT(*) AS rate
                FROM sessions s GROUP BY 1, 2
            )
            SELECT r.month, r.store_name, r.rate
            FROM output_rates r JOIN predicate_rates p USING (store_name, month)
            WHERE p.rate > 0.9 ORDER BY 1, 2
            """
        ).fetchall()
        assert expected == [(datetime(2016, 9, 1), "Philadelphia", 1.0)]
        result = conn.execute(compile_query(config, None, example["query"])["sql"])
        columns = [column[0] for column in result.description]
        rows = [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
        assert [
            (
                row["temporal_role.jaffle_session_started_at__month"],
                row["dimension.jaffle_store_name"],
                row["signup_to_send_28d"],
            )
            for row in rows
        ] == expected


def _orders_on(role):
    return {"measure": "measure.clocks.orders", "temporal_role": role}


@pytest.fixture()
def warehouse(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "metrics").mkdir()
    files = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "clocks",
                "namespace": "clocks",
                "warehouse": "duckdb",
                "default_db": "clocks.duckdb",
                "seed": {"kind": "external"},
            },
        },
        "graph.yml": {
            "graph": {
                "entities": {
                    "customer": {"key": ["customer_id"], "model": "customers"},
                    "order": {"key": ["order_id"], "model": "orders"},
                }
            }
        },
        "models/customers.yml": {
            "model": {
                "id": "customers",
                "relation": "customers",
                "entities": {"customer": {}},
                "dimensions": {"customer_id": {"kind": "categorical"}},
                "times": {"signed_up_at": {"column": "signed_up_at", "default": True}},
                "measures": {
                    "customers": {"kind": "entity_count", "entity_key": "customer_id"},
                    "customer_population": {
                        "kind": "entity_count",
                        "entity_key": "customer_id",
                        "accumulation": "population",
                    },
                },
            }
        },
        "models/orders.yml": {
            "model": {
                "id": "orders",
                "relation": "orders",
                "entities": {"order": {}, "customer": {}},
                "times": {
                    "ordered_at": {"column": "ordered_at"},
                    "shipped_at": {"column": "shipped_at", "default": True},
                },
                "measures": {
                    "orders": {
                        "kind": "entity_count",
                        "entity_key": "order_id",
                        "times": ["shipped_at", "ordered_at"],
                    },
                    "default_orders": {"kind": "entity_count", "entity_key": "order_id"},
                    "authored_default_orders": {
                        "kind": "entity_count",
                        "entity_key": "order_id",
                        "times": ["ordered_at", "shipped_at"],
                        "default_temporal_role": SHIPPED,
                    },
                    "ordered_orders": {
                        "kind": "entity_count",
                        "entity_key": "order_id",
                        "times": ["ordered_at"],
                    },
                },
            }
        },
        "metrics/orders.yml": {
            "metrics": {
                name: {
                    "kind": "derived",
                    "compatible_temporal_roles": [ORDERED, SHIPPED],
                    "expression": expression,
                }
                for name, expression in {
                    "pinned_orders": _orders_on(SHIPPED),
                    "unpinned_orders": {"measure": "measure.clocks.orders"},
                    # Each operand is pinned to its own clock; the metric still advertises both.
                    "mixed_sum": {
                        "kind": "arithmetic",
                        "op": "+",
                        "left": _orders_on(ORDERED),
                        "right": _orders_on(SHIPPED),
                    },
                    "mixed_difference": {
                        "kind": "arithmetic",
                        "op": "-",
                        "left": _orders_on(ORDERED),
                        "right": _orders_on(SHIPPED),
                    },
                    "mixed_ratio": {
                        "kind": "ratio",
                        "numerator": _orders_on(ORDERED),
                        "denominator": _orders_on(SHIPPED),
                    },
                }.items()
            }
        },
        "metrics/conversions.yml": {
            "metrics": {
                "signup_to_order_7d": {
                    "kind": "conversion",
                    "temporal_role": SIGNED_UP,
                    "expression": {
                        "kind": "conversion",
                        "entity": "entity.clocks_customer",
                        "window": {"unit": "day", "value": 7},
                        "matching_mode": "first_converted_after_base",
                        "base": {"kind": "aggregate", "measure": "measure.clocks.customers"},
                        "converted": {"kind": "aggregate", "measure": "measure.clocks.orders"},
                    },
                }
            }
        },
    }
    for name, contents in files.items():
        (tmp_path / name).write_text(yaml.safe_dump(contents, sort_keys=False))
    config = load_package_config(str(tmp_path))
    with duckdb.connect() as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER, signed_up_at TIMESTAMP)")
        conn.execute("INSERT INTO customers VALUES (1, '2025-01-05'), (2, '2025-01-06')")
        conn.execute(
            "CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, "
            "ordered_at TIMESTAMP, shipped_at TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO orders VALUES (1, 1, '2025-01-10', '2025-02-10'), "
            "(2, 2, '2024-12-10', '2025-01-10')"
        )
        yield config, conn


def _query(input_=None, alignment="query_window"):
    return {
        "select": [{"expression": {"measure": "measure.clocks.customers"}, "as": "n"}],
        "group_by": ["dimension.clocks_customer_customer_id"],
        "time": {"temporal_role": SIGNED_UP, "start": "2025-01-01", "end": "2025-02-01"},
        "metric_filters": [
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.clocks_customer",
                    "scope_mode": "entity_only",
                    "time_alignment": alignment,
                    "input": input_ or {"measure": "measure.clocks.orders"},
                    "op": ">=",
                    "value": 1,
                },
                "op": "=",
                "value": True,
            }
        ],
    }


def _reference(conn, clock):
    assert clock in {"ordered_at", "shipped_at"}
    return conn.execute(
        "SELECT c.customer_id, c.signed_up_at, COUNT(*) FROM customers c "
        "WHERE c.signed_up_at >= TIMESTAMP '2025-01-01' "
        "AND c.signed_up_at < TIMESTAMP '2025-02-01' "
        "AND EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id "
        f"AND o.{clock} >= TIMESTAMP '2025-01-01' "
        f"AND o.{clock} < TIMESTAMP '2025-02-01') GROUP BY 1, 2 ORDER BY 1"
    ).fetchall()


def _order_reference(conn, clock, predicate_clock):
    assert clock in {"ordered_at", "shipped_at"}
    assert predicate_clock in {"ordered_at", "shipped_at"}
    return conn.execute(
        f"SELECT o.customer_id, o.{clock}, COUNT(*) FROM orders o "
        f"WHERE o.{clock} >= TIMESTAMP '2025-01-01' "
        f"AND o.{clock} < TIMESTAMP '2025-02-01' "
        "AND EXISTS (SELECT 1 FROM orders p WHERE p.customer_id = o.customer_id "
        f"AND p.{predicate_clock} >= TIMESTAMP '2025-01-01' "
        f"AND p.{predicate_clock} < TIMESTAMP '2025-02-01') "
        "GROUP BY 1, 2 ORDER BY 1"
    ).fetchall()


def _metric_order_query(binding, alignment, role):
    name = "pinned_orders" if binding == "input" else "unpinned_orders"
    query = _query({"metric": f"metric.clocks.{name}"}, alignment)
    query["time"]["temporal_role"] = role
    query["select"] = [
        {"expression": {"measure": "measure.clocks.orders", "temporal_role": role}, "as": "n"}
    ]
    if binding == "override":
        query["temporal_role_overrides"] = {"measure.clocks.orders": SHIPPED}
    return query


def _mixed_query(name, threshold, alignment):
    query = _metric_order_query("input", alignment, ORDERED)
    query["metric_filters"][0]["expression"]["input"] = {"metric": f"metric.clocks.{name}"}
    query["metric_filters"][0]["expression"]["value"] = threshold
    return query


def _mixed_reference(conn, combined, threshold):
    """January orders on ORDERED, for customers whose input counts each operand on its own clock."""
    counts = {
        clock: f"COUNT(*) FILTER (WHERE p.{clock}_at >= TIMESTAMP '2025-01-01' "
        f"AND p.{clock}_at < TIMESTAMP '2025-02-01')"
        for clock in ["ordered", "shipped"]
    }
    return conn.execute(
        "SELECT o.customer_id, o.ordered_at, COUNT(*) FROM orders o "
        "WHERE o.ordered_at >= TIMESTAMP '2025-01-01' "
        "AND o.ordered_at < TIMESTAMP '2025-02-01' "
        "AND o.customer_id IN (SELECT p.customer_id FROM orders p GROUP BY 1 "
        f"HAVING {combined.format(**counts)} >= {threshold}) "
        "GROUP BY 1, 2 ORDER BY 1"
    ).fetchall()


# The thresholds and reference answers at which filtering both operands on ORDERED differs.
MIXED_CASES = [
    pytest.param("mixed_sum", 2, "{ordered} + {shipped}", [], id="sum"),
    pytest.param(
        "mixed_difference",
        1,
        "{ordered} - {shipped}",
        [(1, datetime(2025, 1, 10), 1)],
        id="difference",
    ),
    pytest.param("mixed_ratio", 1, "{ordered} / NULLIF({shipped}, 0)", [], id="ratio"),
]


@pytest.mark.parametrize("alignment", ["query_window", "rolling_window_in_period"])
@pytest.mark.parametrize("entrypoint", [compile_query, _compile_query_sql_ast])
def test_ambiguous_window_clock_refuses_with_candidates(warehouse, alignment, entrypoint):
    config, conn = warehouse
    ordered = _reference(conn, "ordered_at")
    shipped = _reference(conn, "shipped_at")
    assert [row[0] for row in ordered] == [1]
    assert [row[0] for row in shipped] == [2]
    with pytest.raises(SemanticLayerError) as raised:
        if entrypoint is compile_query:
            entrypoint(config, None, _query(alignment=alignment))
        else:
            entrypoint(config, _query(alignment=alignment))
    error = raised.value
    assert error.code == "INVALID_TEMPORAL_BINDING"
    assert error.details["requested"] == SIGNED_UP
    assert error.details["compatible"] == [ORDERED, SHIPPED]
    assert all(clock in str(error) for clock in [ORDERED, SHIPPED])
    hint = error.details["recovery_hints"][0]
    assert hint["code"] == "CHOOSE_PREDICATE_CLOCK"
    assert hint["message"] == (
        "Set the predicate input's temporal_role to a candidate "
        "clock, or set temporal_role_overrides for its measures. "
        "Omit time_alignment to apply the predicate over all time."
    )


@pytest.mark.parametrize("alignment", ["query_window", "rolling_window_in_period"])
@pytest.mark.parametrize("entrypoint", [compile_query, _compile_query_sql_ast])
def test_measure_default_does_not_choose_an_ambiguous_window_clock(
    warehouse, alignment, entrypoint
):
    config, _conn = warehouse
    measure = next(m for m in config.measures if m.id == "measure.clocks.authored_default_orders")
    assert measure.compatible_temporal_roles == [ORDERED, SHIPPED]
    assert measure.default_temporal_role == SHIPPED
    query = _query({"measure": measure.id}, alignment)
    with pytest.raises(SemanticLayerError) as raised:
        if entrypoint is compile_query:
            entrypoint(config, None, query)
        else:
            entrypoint(config, query)
    assert raised.value.code == "INVALID_TEMPORAL_BINDING"
    assert raised.value.details["requested"] == SIGNED_UP
    assert raised.value.details["compatible"] == [ORDERED, SHIPPED]
    assert all(clock in str(raised.value) for clock in [ORDERED, SHIPPED])


@pytest.mark.parametrize("alignment", ["query_window", "rolling_window_in_period"])
@pytest.mark.parametrize("binding", ["input", "override"])
@pytest.mark.parametrize("entrypoint", [compile_query, _compile_query_sql_ast])
def test_metric_advertised_clocks_refuse_despite_measure_binding(
    warehouse, alignment, binding, entrypoint
):
    config, _conn = warehouse
    name = "pinned_orders" if binding == "input" else "unpinned_orders"
    query = _query({"metric": f"metric.clocks.{name}"}, alignment)
    if binding == "override":
        query["temporal_role_overrides"] = {"measure.clocks.orders": SHIPPED}
    with pytest.raises(SemanticLayerError) as raised:
        if entrypoint is compile_query:
            entrypoint(config, None, query)
        else:
            entrypoint(config, query)
    error = raised.value
    assert error.code == "INVALID_TEMPORAL_BINDING"
    assert error.details["requested"] == SIGNED_UP
    assert error.details["compatible"] == [ORDERED, SHIPPED]
    assert all(clock in str(error) for clock in [ORDERED, SHIPPED])
    hint = error.details["recovery_hints"][0]
    assert hint["code"] == "CHOOSE_PREDICATE_CLOCK"
    assert hint["message"] == (
        "Set query.time.temporal_role to one of the listed clocks. "
        "Omit time_alignment to apply the predicate over all time."
    )


@pytest.mark.parametrize("alignment", ["query_window", "rolling_window_in_period"])
@pytest.mark.parametrize("binding", ["input", "override"])
@pytest.mark.parametrize("entrypoint", [compile_query, _compile_query_sql_ast])
def test_metric_bound_clock_conflicting_with_window_clock_refuses(
    warehouse, alignment, binding, entrypoint
):
    config, conn = warehouse
    assert _order_reference(conn, "ordered_at", "shipped_at") == []
    query = _metric_order_query(binding, alignment, ORDERED)
    with pytest.raises(SemanticLayerError) as raised:
        if entrypoint is compile_query:
            entrypoint(config, None, query)
        else:
            entrypoint(config, query)
    error = raised.value
    assert error.code == "INVALID_TEMPORAL_BINDING"
    assert error.details["requested"] == ORDERED
    assert error.details["compatible"] == [SHIPPED]
    hint = error.details["recovery_hints"][0]
    assert hint["code"] == "CHOOSE_PREDICATE_CLOCK"
    assert SHIPPED in hint["message"]
    assert "query.time.temporal_role" in hint["message"]
    assert "omit time_alignment" in hint["message"]


@pytest.mark.parametrize("alignment", ["query_window", "rolling_window_in_period"])
@pytest.mark.parametrize("name,threshold,combined,expected", MIXED_CASES)
@pytest.mark.parametrize("entrypoint", [compile_query, _compile_query_sql_ast])
def test_window_clock_excluded_by_any_measure_in_metric_refuses(
    warehouse, alignment, name, threshold, combined, expected, entrypoint
):
    config, conn = warehouse
    assert _mixed_reference(conn, combined, threshold) == expected
    query = _mixed_query(name, threshold, alignment)
    with pytest.raises(SemanticLayerError) as raised:
        if entrypoint is compile_query:
            entrypoint(config, None, query)
        else:
            entrypoint(config, query)
    error = raised.value
    assert error.code == "INVALID_TEMPORAL_BINDING"
    assert error.details["requested"] == ORDERED
    assert error.details["compatible"] == [SHIPPED]
    hint = error.details["recovery_hints"][0]
    assert hint["code"] == "CHOOSE_PREDICATE_CLOCK"
    assert hint["message"] == (
        f"The input is bound to {SHIPPED}; set query.time.temporal_role to one of them, "
        "or omit time_alignment to apply the predicate over all time."
    )


def test_contextual_window_clock_excluded_by_any_measure_in_metric_refuses(warehouse):
    config, conn = warehouse
    assert _mixed_reference(conn, "{ordered} + {shipped}", 2) == []
    query = _mixed_query("mixed_sum", 2, "same_query_period")
    query["metric_filters"][0]["expression"]["scope_mode"] = "contextual"
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, None, query)
    assert raised.value.code == "INVALID_TEMPORAL_BINDING"
    assert raised.value.details["requested"] == ORDERED
    assert raised.value.details["compatible"] == [SHIPPED]


def test_contextual_metric_bound_clock_conflict_refuses(warehouse):
    config, conn = warehouse
    assert _order_reference(conn, "ordered_at", "shipped_at") == []
    query = _metric_order_query("input", "same_query_period", ORDERED)
    query["metric_filters"][0]["expression"]["scope_mode"] = "contextual"
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, None, query)
    assert raised.value.code == "INVALID_TEMPORAL_BINDING"
    assert raised.value.details["requested"] == ORDERED
    assert raised.value.details["compatible"] == [SHIPPED]


@pytest.mark.parametrize("alignment", ["query_window", "rolling_window_in_period"])
@pytest.mark.parametrize("binding", ["input", "override"])
def test_metric_bound_clock_agreeing_with_window_clock_matches_reference(
    warehouse, alignment, binding
):
    config, conn = warehouse
    expected = _order_reference(conn, "shipped_at", "shipped_at")
    assert [row[0] for row in expected] == [2]
    query = _metric_order_query(binding, alignment, SHIPPED)
    assert conn.execute(compile_query(config, None, query)["sql"]).fetchall() == expected


def test_direct_lowering_cannot_bypass_metric_bound_clock_refusal(warehouse):
    config, _conn = warehouse
    plan = plan_query(config, None, _metric_order_query("input", "query_window", SHIPPED))
    unsafe = _metric_order_query("input", "query_window", ORDERED)
    with pytest.raises(SemanticLayerError) as raised:
        lower_to_sql(replace(plan, query=unsafe), config)
    assert raised.value.code == "INVALID_TEMPORAL_BINDING"
    assert raised.value.details["requested"] == ORDERED
    assert raised.value.details["compatible"] == [SHIPPED]


def test_direct_lowering_cannot_bypass_any_measure_bound_clock_refusal(warehouse):
    config, _conn = warehouse
    plan = plan_query(config, None, _metric_order_query("input", "query_window", SHIPPED))
    with pytest.raises(SemanticLayerError) as raised:
        lower_to_sql(replace(plan, query=_mixed_query("mixed_sum", 2, "query_window")), config)
    assert raised.value.code == "INVALID_TEMPORAL_BINDING"
    assert raised.value.details["requested"] == ORDERED
    assert raised.value.details["compatible"] == [SHIPPED]


@pytest.mark.parametrize("alignment", ["query_window", "rolling_window_in_period"])
@pytest.mark.parametrize("clock,role", [("ordered_at", ORDERED), ("shipped_at", SHIPPED)])
@pytest.mark.parametrize("binding", ["input", "override"])
def test_explicit_window_clock_matches_reference(warehouse, alignment, clock, role, binding):
    config, conn = warehouse
    query = _query(alignment=alignment)
    if binding == "input":
        query["metric_filters"][0]["expression"]["input"]["temporal_role"] = role
    else:
        query["temporal_role_overrides"] = {"measure.clocks.orders": role}
    assert conn.execute(compile_query(config, None, query)["sql"]).fetchall() == _reference(
        conn, clock
    )


@pytest.mark.parametrize("alignment", ["query_window", "rolling_window_in_period"])
@pytest.mark.parametrize(
    "measure,clock", [("ordered_orders", "ordered_at"), ("default_orders", "shipped_at")]
)
def test_single_clock_declared_or_inherited_from_model_default_time_matches_reference(
    warehouse, alignment, measure, clock
):
    config, conn = warehouse
    query = _query({"measure": f"measure.clocks.{measure}"}, alignment)
    assert conn.execute(compile_query(config, None, query)["sql"]).fetchall() == _reference(
        conn, clock
    )


def test_direct_lowering_cannot_bypass_clock_refusal(warehouse):
    config, _conn = warehouse
    safe = _query({"measure": "measure.clocks.orders", "temporal_role": SHIPPED})
    plan = plan_query(config, None, safe)
    with pytest.raises(SemanticLayerError) as raised:
        lower_to_sql(replace(plan, query=_query()), config)
    assert raised.value.code == "INVALID_TEMPORAL_BINDING"


def _conversion_query(alignment, override):
    query = _query({"metric": "metric.clocks.signup_to_order_7d"}, alignment)
    predicate = query["metric_filters"][0]["expression"]
    predicate.update(op=">", value=0.5)
    if alignment is None:
        del predicate["time_alignment"]
    elif alignment == "same_query_period":
        predicate["scope_mode"] = "contextual"
    if override:
        query["temporal_role_overrides"] = {"measure.clocks.orders": ORDERED}
    return query


def _converted_customers(clock):
    """Customers whose 7-day signup-to-order rate, with orders on ``clock``, is above 0.5."""
    assert clock in {"ordered_at", "shipped_at"}
    return (
        "SELECT b.customer_id FROM customers b GROUP BY 1 HAVING "
        "AVG(CASE WHEN EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = b.customer_id "
        f"AND o.{clock} >= b.signed_up_at AND o.{clock} < b.signed_up_at + INTERVAL 7 DAY) "
        "THEN 1.0 ELSE 0.0 END) > 0.5"
    )


def _conversion_reference(conn, clock):
    return conn.execute(
        "SELECT c.customer_id, c.signed_up_at, COUNT(*) FROM customers c "
        "WHERE c.signed_up_at >= TIMESTAMP '2025-01-01' "
        "AND c.signed_up_at < TIMESTAMP '2025-02-01' "
        f"AND c.customer_id IN ({_converted_customers(clock)}) GROUP BY 1, 2 ORDER BY 1"
    ).fetchall()


def _runtime(tmp_path, conn):
    conn.execute(f"ATTACH '{tmp_path / 'clocks.duckdb'}' AS copy")
    conn.execute("COPY FROM DATABASE memory TO copy")
    conn.execute("DETACH copy")
    return Runtime.from_path(str(tmp_path))


def _assert_override_refusal(error):
    assert error.code == "INVALID_TEMPORAL_BINDING"
    assert error.details["measures"] == ["measure.clocks.orders"]
    assert "measure 'measure.clocks.orders'" in str(error)
    assert error.details["recovery_hints"][0]["message"] == (
        "Remove the override for this measure, or use a metric whose definition binds that clock."
    )


CONVERSION_ALIGNMENTS = ["query_window", "rolling_window_in_period", "same_query_period", None]


@pytest.mark.parametrize("alignment", CONVERSION_ALIGNMENTS)
@pytest.mark.parametrize("entrypoint", ["runtime", "compile"])
def test_conversion_input_refuses_overridden_converted_measure(
    warehouse, tmp_path, alignment, entrypoint
):
    config, conn = warehouse
    assert [row[0] for row in _conversion_reference(conn, "ordered_at")] == [1]
    assert [row[0] for row in _conversion_reference(conn, "shipped_at")] == [2]
    query = _conversion_query(alignment, override=True)
    with pytest.raises(SemanticLayerError) as raised:
        if entrypoint == "runtime":
            runtime = _runtime(tmp_path, conn)
            try:
                runtime.query(query)
            finally:
                runtime.close()
        else:
            _compile_query_sql_ast(config, query)
    _assert_override_refusal(raised.value)


@pytest.mark.parametrize("alignment", CONVERSION_ALIGNMENTS)
def test_conversion_input_without_override_matches_reference(warehouse, tmp_path, alignment):
    config, conn = warehouse
    expected = _conversion_reference(conn, "shipped_at")
    assert [row[0] for row in expected] == [2]
    query = _conversion_query(alignment, override=False)
    assert conn.execute(compile_query(config, None, query)["sql"]).fetchall() == expected
    runtime = _runtime(tmp_path, conn)
    try:
        result = runtime.query(query)
    finally:
        runtime.close()
    assert [tuple(row.values()) for row in result["rows"]] == [
        (customer, signed_up.isoformat(), n) for customer, signed_up, n in expected
    ]


def test_direct_lowering_cannot_bypass_override_refusal(warehouse):
    config, _conn = warehouse
    plan = plan_query(config, None, _conversion_query("query_window", override=False))
    unsafe = _conversion_query("query_window", override=True)
    with pytest.raises(SemanticLayerError) as raised:
        lower_to_sql(replace(plan, query=unsafe), config)
    _assert_override_refusal(raised.value)


def _share_query(override):
    scoped = {
        "kind": "scoped_aggregate",
        "measure": "measure.clocks.customer_population",
        "aggregation": "count_distinct",
    }
    predicate = {
        "input": {"metric": "metric.clocks.signup_to_order_7d"},
        "entity": "entity.clocks_customer",
        "op": ">",
        "value": 0.5,
    }
    query = {
        "select": [
            {
                "expression": {
                    "kind": "ratio",
                    "numerator": {**scoped, "predicates": [predicate]},
                    "denominator": scoped,
                },
                "as": "share",
            }
        ],
        "time": {"temporal_role": SIGNED_UP, "start": "2025-01-01", "end": "2025-02-01"},
    }
    if override:
        query["temporal_role_overrides"] = {"measure.clocks.orders": ORDERED}
    return query


def _share_reference(conn, clock):
    return conn.execute(
        "SELECT c.signed_up_at, AVG(CASE WHEN c.customer_id IN "
        f"({_converted_customers(clock)}) THEN 1.0 ELSE 0.0 END) FROM customers c "
        "WHERE c.signed_up_at >= TIMESTAMP '2025-01-01' "
        "AND c.signed_up_at < TIMESTAMP '2025-02-01' GROUP BY 1 ORDER BY 1"
    ).fetchall()


def test_anchored_ratio_cannot_bypass_override_refusal(warehouse, monkeypatch):
    """This ratio builds its predicate set itself, without the predicate scope builder."""
    config, conn = warehouse
    expected = _share_reference(conn, "shipped_at")
    assert expected == [(datetime(2025, 1, 5), 0), (datetime(2025, 1, 6), 1)]
    assert _share_reference(conn, "ordered_at") == [
        (datetime(2025, 1, 5), 1),
        (datetime(2025, 1, 6), 0),
    ]
    built = []
    build = sql_lowering._minimal_predicate_set_ctes

    def spy(*args, **kwargs):
        built.append(kwargs["index"])
        return build(*args, **kwargs)

    monkeypatch.setattr(sql_lowering, "_minimal_predicate_set_ctes", spy)
    sql = compile_query(config, None, _share_query(override=False))["sql"]
    assert conn.execute(sql).fetchall() == expected
    assert len(built) == 1
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, None, _share_query(override=True))
    assert len(built) == 2
    _assert_override_refusal(raised.value)


def _output_query(name, role, grain=""):
    query = _query()
    query.pop("metric_filters")
    query["select"] = [{"expression": {"metric": f"metric.clocks.{name}"}, "as": "n"}]
    query["time"].update(temporal_role=role, grain=grain)
    return query


@pytest.mark.parametrize("grain", ["", "month"])
@pytest.mark.parametrize("entrypoint", ["compile", "nested", "runtime"])
def test_pinned_metric_output_refuses_a_window_on_another_advertised_clock(
    warehouse, tmp_path, grain, entrypoint
):
    config, conn = warehouse
    assert conn.execute(
        "SELECT customer_id, COUNT(*) FROM orders WHERE shipped_at >= TIMESTAMP '2025-01-01' "
        "AND shipped_at < TIMESTAMP '2025-02-01' GROUP BY 1"
    ).fetchall() == [(2, 1)]
    query = _output_query("pinned_orders", ORDERED, grain)
    with pytest.raises(SemanticLayerError) as raised:
        if entrypoint == "runtime":
            runtime = _runtime(tmp_path, conn)
            try:
                report = runtime.validate(query)
                assert not report["ok"]
                assert report["errors"][0]["code"] == "INVALID_TEMPORAL_BINDING"
                runtime.query(query)
            finally:
                runtime.close()
        elif entrypoint == "nested":
            _compile_query_sql_ast(config, query)
        else:
            compile_query(config, None, query)
    error = raised.value
    assert error.code == "INVALID_TEMPORAL_BINDING"
    assert error.details["measure"] == "measure.clocks.orders"
    assert error.details["requested"] == ORDERED
    assert error.details["compatible"] == [SHIPPED]
    assert error.details["recovery_hints"][0]["code"] == "CHOOSE_OUTPUT_CLOCK"
    assert SHIPPED in error.details["recovery_hints"][0]["message"]


@pytest.mark.parametrize(
    "name,role,clock",
    [
        ("pinned_orders", SHIPPED, "shipped_at"),
        ("unpinned_orders", ORDERED, "ordered_at"),
        ("unpinned_orders", SHIPPED, "shipped_at"),
    ],
)
def test_metric_output_on_its_bound_clock_matches_reference(warehouse, name, role, clock):
    config, conn = warehouse
    expected = conn.execute(
        f"SELECT customer_id, COUNT(*) FROM orders WHERE {clock} >= TIMESTAMP '2025-01-01' "
        f"AND {clock} < TIMESTAMP '2025-02-01' GROUP BY 1"
    ).fetchall()
    assert expected == [(1 if role == ORDERED else 2, 1)]
    assert (
        conn.execute(compile_query(config, None, _output_query(name, role))["sql"]).fetchall()
        == expected
    )


def test_direct_lowering_cannot_bypass_pinned_output_clock_refusal(warehouse):
    config, _conn = warehouse
    safe = _output_query("pinned_orders", SHIPPED)
    plan = plan_query(config, None, safe)
    with pytest.raises(SemanticLayerError) as raised:
        lower_to_sql(replace(plan, query=_output_query("pinned_orders", ORDERED)), config)
    assert raised.value.code == "INVALID_TEMPORAL_BINDING"
    assert raised.value.details["compatible"] == [SHIPPED]


@pytest.mark.parametrize("shape", ["arithmetic", "separate-output", "metric-filter", "override"])
def test_composed_output_cannot_hide_a_conflicting_bound_clock(warehouse, shape):
    config, _conn = warehouse
    query = _output_query("pinned_orders", ORDERED, "month")
    pinned = query["select"][0]["expression"]
    unpinned = {"metric": "metric.clocks.unpinned_orders"}
    if shape == "arithmetic":
        query["select"][0]["expression"] = {
            "kind": "arithmetic",
            "op": "+",
            "left": pinned,
            "right": unpinned,
        }
    elif shape == "separate-output":
        query["select"].append({"expression": unpinned, "as": "other"})
    elif shape == "metric-filter":
        query["select"][0]["expression"] = unpinned
        query["metric_filters"] = [{"expression": pinned, "op": ">", "value": 0}]
    else:
        query["select"][0]["expression"] = unpinned
        query["temporal_role_overrides"] = {"measure.clocks.orders": SHIPPED}
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, None, query)
    assert raised.value.code == "INVALID_TEMPORAL_BINDING"
    assert raised.value.details["compatible"] == [SHIPPED]


def _nested_query(alignment, override, depth=1, input_=None):
    query = _query(alignment=alignment)
    predicate = {
        "input": input_ or {"metric": "metric.clocks.signup_to_order_7d"},
        "entity": "entity.clocks_customer",
        "scope_mode": "entity_only",
        "op": ">",
        "value": 0.5,
    }
    for _ in range(depth):
        scoped = {
            "kind": "scoped_aggregate",
            "measure": "measure.clocks.customers",
            "aggregation": "count_distinct",
            "predicates": [predicate],
        }
        predicate = {**predicate, "input": scoped, "op": ">=", "value": 1}
    query["metric_filters"][0]["expression"]["input"] = scoped
    if alignment is None:
        del query["metric_filters"][0]["expression"]["time_alignment"]
    elif alignment == "same_query_period":
        query["metric_filters"][0]["expression"]["scope_mode"] = "contextual"
    if override:
        query["temporal_role_overrides"] = {"measure.clocks.orders": ORDERED}
    return query


@pytest.mark.parametrize("alignment", CONVERSION_ALIGNMENTS)
@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("entrypoint", ["runtime", "compile"])
def test_nested_scoped_predicate_refuses_an_override_its_input_would_drop(
    warehouse, tmp_path, alignment, depth, entrypoint
):
    config, conn = warehouse
    assert [row[0] for row in _conversion_reference(conn, "ordered_at")] == [1]
    assert [row[0] for row in _conversion_reference(conn, "shipped_at")] == [2]
    query = _nested_query(alignment, override=True, depth=depth)
    with pytest.raises(SemanticLayerError) as raised:
        if entrypoint == "runtime":
            runtime = _runtime(tmp_path, conn)
            try:
                runtime.query(query)
            finally:
                runtime.close()
        else:
            _compile_query_sql_ast(config, query)
    _assert_override_refusal(raised.value)


@pytest.mark.parametrize("alignment", CONVERSION_ALIGNMENTS)
@pytest.mark.parametrize("depth", [1, 2])
def test_nested_scoped_predicate_without_override_matches_reference(
    warehouse, tmp_path, alignment, depth
):
    config, conn = warehouse
    expected = _conversion_reference(conn, "shipped_at")
    query = _nested_query(alignment, override=False, depth=depth)
    assert conn.execute(compile_query(config, None, query)["sql"]).fetchall() == expected
    runtime = _runtime(tmp_path, conn)
    try:
        result = runtime.query(query)
    finally:
        runtime.close()
    assert [tuple(row.values()) for row in result["rows"]] == [
        (customer, signed_up.isoformat(), n) for customer, signed_up, n in expected
    ]


def test_direct_lowering_cannot_bypass_nested_override_refusal(warehouse):
    config, _conn = warehouse
    plan = plan_query(config, None, _nested_query("query_window", override=False))
    with pytest.raises(SemanticLayerError) as raised:
        lower_to_sql(replace(plan, query=_nested_query("query_window", override=True)), config)
    _assert_override_refusal(raised.value)


@pytest.mark.parametrize("depth", [1, 2])
def test_nested_scoped_predicate_still_refuses_an_ambiguous_window_clock(warehouse, depth):
    config, conn = warehouse
    assert [row[0] for row in _reference(conn, "ordered_at")] == [1]
    assert [row[0] for row in _reference(conn, "shipped_at")] == [2]
    query = _nested_query(
        "same_query_period",
        override=False,
        depth=depth,
        input_={"measure": "measure.clocks.orders"},
    )
    scoped = query["metric_filters"][0]["expression"]["input"]
    for _ in range(depth):
        predicate = scoped["predicates"][0]
        predicate.update(scope_mode="contextual", time_alignment="same_query_period")
        scoped = predicate["input"]
    predicate.update(scope_mode="entity_only", time_alignment="query_window")
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, None, query)
    assert raised.value.code == "INVALID_TEMPORAL_BINDING"
    assert raised.value.details["compatible"] == [ORDERED, SHIPPED]


NESTED_AGGREGATES = [
    ("aggregate",),
    ("aggregate", "aggregate"),
    ("aggregate", "scoped_aggregate"),
    ("scoped_aggregate", "aggregate"),
    ("aggregate", "scoped_aggregate", "aggregate"),
]


def _aggregate_nested_query(nesting, override):
    query = _nested_query("query_window", override=override)
    outer = query["metric_filters"][0]["expression"]
    predicate = outer["input"]["predicates"][0]
    for kind in reversed(nesting):
        aggregate = {
            "kind": kind,
            "measure": "measure.clocks.customers",
            "aggregation": "count_distinct",
        }
        if kind == "aggregate":
            aggregate["filter"] = {
                "all": [{"expression": {"kind": "metric_predicate", **predicate}}]
            }
        else:
            aggregate["predicates"] = [predicate]
        predicate = {**predicate, "input": aggregate, "op": ">=", "value": 1}
    outer["input"] = aggregate
    return query


@pytest.mark.parametrize("nesting", NESTED_AGGREGATES)
@pytest.mark.parametrize("entrypoint", ["validate", "compile", "lower"])
def test_aggregate_filter_predicates_refuse_an_override_their_input_would_drop(
    warehouse, tmp_path, nesting, entrypoint
):
    config, conn = warehouse
    assert _conversion_reference(conn, "ordered_at") == [(1, datetime(2025, 1, 5), 1)]
    assert _conversion_reference(conn, "shipped_at") == [(2, datetime(2025, 1, 6), 1)]
    query = _aggregate_nested_query(nesting, override=True)
    if entrypoint == "lower":
        plan = plan_query(config, None, _aggregate_nested_query(nesting, override=False))
        with pytest.raises(SemanticLayerError) as raised:
            lower_to_sql(replace(plan, query=query), config)
        _assert_override_refusal(raised.value)
        return
    runtime = _runtime(tmp_path, conn)
    try:
        if entrypoint == "validate":
            report = runtime.validate(query)
            assert not report["ok"], report
            assert report["errors"][0]["code"] == "INVALID_TEMPORAL_BINDING"
            assert report["errors"][0]["details"]["measures"] == ["measure.clocks.orders"]
        else:
            with pytest.raises(SemanticLayerError) as raised:
                runtime.compile(query)
            _assert_override_refusal(raised.value)
    finally:
        runtime.close()


@pytest.mark.parametrize("nesting", NESTED_AGGREGATES)
def test_aggregate_filter_predicates_without_override_keep_the_reference_answer(warehouse, nesting):
    config, conn = warehouse
    query = _aggregate_nested_query(nesting, override=False)
    assert conn.execute(compile_query(config, None, query)["sql"]).fetchall() == (
        _conversion_reference(conn, "shipped_at")
    )


@pytest.mark.parametrize("connective", ["all", "any", "not", "mixed"])
def test_typed_predicate_guard_walks_only_filter_expression_positions(warehouse, connective):
    config, _conn = warehouse
    query = _nested_query("query_window", override=True)
    outer = query["metric_filters"][0]["expression"]
    inner = {"kind": "metric_predicate", **outer["input"]["predicates"][0]}
    clause = {"expression": inner}
    filter_spec = (
        {"all": [{"not": {"any": [clause]}}]}
        if connective == "mixed"
        else {connective: clause if connective == "not" else [clause]}
    )
    # Public parsing still refuses unsupported connectives. A typed input must
    # not let those expression positions bypass the temporal override guard.
    predicate = parse_semantic_expression(outer, context="query")
    predicate = replace(
        predicate,
        input=AggregateExpr(measure="measure.clocks.customers", filter=filter_spec),
    )
    with pytest.raises(SemanticLayerError) as raised:
        _refuse_overridden_predicate_input(predicate, normalize_query(query), config)
    _assert_override_refusal(raised.value)
    data_filter = {
        "all": [{"field": "dimension.clocks_customer_customer_id", "value": filter_spec}]
    }
    predicate = replace(predicate, input=replace(predicate.input, filter=data_filter))
    _refuse_overridden_predicate_input(predicate, normalize_query(query), config)


@pytest.mark.parametrize("entrypoint", ["binding", "metadata"])
def test_pinned_rolling_metric_metadata_uses_its_second_advertised_clock(entrypoint):
    config = load_package_config("tests/integration/correctness/shop")
    metric_id = "metric.shop.closed_orders"
    recipe = next(recipe for recipe in config.metric_recipes if recipe.id == metric_id)
    config.metric_recipes[:] = [
        replace(
            recipe,
            expression=parse_semantic_expression(
                {
                    "kind": "rolling",
                    "input": {
                        "measure": "measure.shop.window_order_count",
                        "temporal_role": "temporal_role.shop_order_closed_at",
                    },
                    "window": {"unit": "day", "value": 7},
                },
                context="query",
            ),
        )
        if item.id == metric_id
        else item
        for item in config.metric_recipes
    ]
    if entrypoint == "binding":
        references = bind_metadata_objects(config, [metric_id])
        assert {
            metric_id,
            "measure.shop.window_order_count",
            "temporal_role.shop_order_closed_at",
        } <= references
    else:
        access = ResourceAccess(config, RequestContext(metric_allowlist=(metric_id,)))
        assert metric_id in {row["id"] for row in access.visible_rows()}


@pytest.mark.parametrize("metric", ["mixed_sum", "unpinned_orders"])
def test_metadata_clock_keeps_advertised_default_without_one_common_leaf_clock(warehouse, metric):
    config, _conn = warehouse
    assert _object_default_query_temporal_role(config, f"metric.clocks.{metric}") == ORDERED
