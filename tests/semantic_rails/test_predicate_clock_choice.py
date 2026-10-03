"""A bounded entity predicate needs an unambiguous clock for its window."""

from dataclasses import replace

import duckdb
import pytest
import yaml

from semantic_rails.compiler import _compile_query_sql_ast, compile_query, lower_to_sql, plan_query
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError

ORDERED = "temporal_role.clocks_order_ordered_at"
SHIPPED = "temporal_role.clocks_order_shipped_at"
SIGNED_UP = "temporal_role.clocks_customer_signed_up_at"


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
                "measures": {"customers": {"kind": "entity_count", "entity_key": "customer_id"}},
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
                    "expression": {
                        "measure": "measure.clocks.orders",
                        **({"temporal_role": SHIPPED} if name == "pinned_orders" else {}),
                    },
                }
                for name in ["pinned_orders", "unpinned_orders"]
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
