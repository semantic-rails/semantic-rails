"""Prepared-statement parameters: typed slots bound per request, driver-side values, no fallback."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import duckdb
import pytest

from semantic_rails import runtime as runtime_module
from semantic_rails.db import DuckDBAdapter, WarehouseAdapter
from semantic_rails.db_parts.athena import AthenaAdapter
from semantic_rails.db_parts.bigquery import BigQueryNativeAdapter
from semantic_rails.db_parts.clickhouse import ClickHouseAdapter
from semantic_rails.db_parts.databricks import DatabricksNativeAdapter
from semantic_rails.db_parts.ducklake import DuckLakeAdapter
from semantic_rails.db_parts.motherduck import MotherDuckAdapter
from semantic_rails.db_parts.postgres import PostgresAdapter
from semantic_rails.db_parts.snowflake import SnowflakeCliAdapter, SnowflakeNativeAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import _adapter_query
from semantic_rails.sql_preparation import ParameterSlot, PreparedQuery

INJECTION = "c-a' OR '1'='1"
CANARY = "canary-e2-5519"
LEDGER = [
    ("c-a", 1, True, 10),
    ("c-a", 1, True, 5),
    ("c-b", 2, False, 7),
    (INJECTION, 3, True, 1000),
]
TOTALS = PreparedQuery(
    'SELECT COUNT(*) AS "n", SUM(amount) AS "total" FROM ledger WHERE customer_id = ?',
    parameters=(ParameterSlot("customer_id", "string"),),
)


def _context(**attributes):
    """A policy context as an embedding host builds it from verified identity."""
    return RequestContext(actor="host-user", attributes=attributes).to_policy_context()


def _forbidden(*args, **kwargs):
    raise AssertionError("the statement must not reach the driver")


@pytest.fixture
def ledger(tmp_path):
    path = str(tmp_path / "ledger.duckdb")
    with duckdb.connect(path) as conn:
        conn.execute(
            "CREATE TABLE ledger (customer_id VARCHAR, tier INTEGER, active BOOLEAN, amount INTEGER)"
        )
        conn.executemany("INSERT INTO ledger VALUES (?, ?, ?, ?)", LEDGER)
    adapter = DuckDBAdapter(path)
    yield adapter
    adapter.close()


def _record_driver_calls(adapter, monkeypatch):
    sent = []
    real = adapter._db.query

    def query(sql, params=None, **kwargs):
        sent.append((sql, tuple(params or ())))
        return real(sql, params, **kwargs)

    monkeypatch.setattr(adapter._db, "query", query)
    return sent


def test_a_and_b_share_one_statement_with_isolated_bindings(ledger, monkeypatch):
    sent = _record_driver_calls(ledger, monkeypatch)
    a = _adapter_query(ledger, TOTALS, limits={}, policy_context=_context(customer_id="c-a"))
    b = _adapter_query(ledger, TOTALS, limits={}, policy_context=_context(customer_id="c-b"))
    assert a == [{"n": 2, "total": 15}]
    assert b == [{"n": 1, "total": 7}]
    assert sent == [(TOTALS.sql, ("c-a",)), (TOTALS.sql, ("c-b",))]


@pytest.mark.parametrize(
    ("column", "slot_type", "value", "count"),
    [
        ("customer_id", "string", "c-a", 2),
        ("tier", "integer", 2, 1),
        ("active", "boolean", True, 3),
    ],
)
def test_each_slot_type_binds_through_duckdb(ledger, column, slot_type, value, count):
    prepared = PreparedQuery(
        f'SELECT COUNT(*) AS "n" FROM ledger WHERE {column} = ?',
        parameters=(ParameterSlot("key", slot_type),),
    )
    assert _adapter_query(ledger, prepared, limits={}, policy_context=_context(key=value)) == [
        {"n": count}
    ]


@pytest.mark.parametrize(
    ("value", "rows"),
    [
        (INJECTION, [{"n": 1, "total": 1000}]),  # only the row whose ID is exactly this text
        ("c-a'; DROP TABLE ledger; --", [{"n": 0, "total": None}]),
        ("c-a\\", [{"n": 0, "total": None}]),
        ("?", [{"n": 0, "total": None}]),
    ],
)
def test_values_are_data_never_statement_text(ledger, monkeypatch, value, rows):
    sent = _record_driver_calls(ledger, monkeypatch)
    result = _adapter_query(ledger, TOTALS, limits={}, policy_context=_context(customer_id=value))
    assert result == rows
    assert sent == [(TOTALS.sql, (value,))]
    assert ledger.query('SELECT COUNT(*) AS "n" FROM ledger') == [{"n": len(LEDGER)}]


def test_a_stray_placeholder_fails_instead_of_moving_a_value(ledger):
    stray = PreparedQuery(
        'SELECT ? AS "leak" FROM ledger WHERE customer_id = ?', parameters=TOTALS.parameters
    )
    with pytest.raises(SemanticLayerError) as caught:
        _adapter_query(ledger, stray, limits={}, policy_context=_context(customer_id="c-a"))
    assert caught.value.code == "QUERY_EXECUTION_ERROR"


@pytest.mark.parametrize(
    ("slot_type", "attributes", "reason"),
    [
        ("string", {}, "missing_attribute"),  # never bound as NULL
        ("string", {"other": CANARY}, "missing_attribute"),
        ("string", {"customer_id": 7}, "attribute_type_mismatch"),
        ("string", {"customer_id": [CANARY]}, "attribute_type_mismatch"),
        ("integer", {"customer_id": True}, "attribute_type_mismatch"),
        ("integer", {"customer_id": CANARY}, "attribute_type_mismatch"),
        ("boolean", {"customer_id": 1}, "attribute_type_mismatch"),
    ],
)
def test_missing_or_mistyped_attributes_deny_before_the_driver(
    ledger, monkeypatch, slot_type, attributes, reason
):
    prepared = PreparedQuery("SELECT ? AS v", parameters=(ParameterSlot("customer_id", slot_type),))
    monkeypatch.setattr(ledger._db, "query", _forbidden)
    with pytest.raises(SemanticLayerError) as caught:
        _adapter_query(ledger, prepared, limits={}, policy_context=_context(**attributes))
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value.details == {"reason": reason, "attribute": "customer_id"}
    assert CANARY not in f"{caught.value} {caught.value.details}"


def test_only_host_attributes_or_exact_values_can_bind(ledger, monkeypatch):
    monkeypatch.setattr(ledger._db, "query", _forbidden)
    denials = [
        # Caller JSON cannot supply a value: only a host-built TrustedAttributes binds.
        lambda: _adapter_query(
            ledger, TOTALS, limits={}, policy_context={"attributes": {"customer_id": "c-a"}}
        ),
        lambda: _adapter_query(ledger, TOTALS, limits={}),
        # Direct adapter calls check the values against the slots too.
        lambda: ledger.query_prepared(TOTALS),
        lambda: ledger.query_prepared(TOTALS, parameters=[None]),
        lambda: ledger.query_prepared(TOTALS, parameters=["c-a", "c-b"]),
        lambda: ledger.query_prepared(PreparedQuery("SELECT 1 AS n"), parameters=["c-a"]),
    ]
    for deny in denials:
        with pytest.raises(SemanticLayerError) as caught:
            deny()
        assert caught.value.code == "POLICY_DENIED"
    with pytest.raises(ValueError):
        ParameterSlot("customer_id", "date")  # type: ignore[arg-type]


def test_concurrent_executions_keep_their_own_bindings(ledger):
    totals = {"c-a": 15, "c-b": 7, INJECTION: 1000}

    def run(customer):
        rows = _adapter_query(
            ledger, TOTALS, limits={}, policy_context=_context(customer_id=customer)
        )
        return customer, rows[0]["total"]

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(run, list(totals) * 40))
    assert all(total == totals[customer] for customer, total in results)
    assert TOTALS.parameters == (ParameterSlot("customer_id", "string"),)


def test_runtime_binds_each_request_from_its_own_trusted_attributes(
    runtime_factory, ledger, monkeypatch
):
    # Stand-in for a compiled row filter: every request shares one cached template.
    real_compile = runtime_module.compile_query
    monkeypatch.setattr(
        runtime_module,
        "compile_query",
        lambda *args, **kwargs: {**real_compile(*args, **kwargs), "prepared_query": TOTALS},
    )
    runtime = runtime_factory("jaffle_shop")
    runtime.set_adapter(ledger)
    request = {"version": 1, "select": [{"expression": {"metric": "metric.sales.aov_usd"}}]}
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(
                pool.map(
                    lambda customer: (
                        customer,
                        runtime.query(
                            {**request, "policy_context": _context(customer_id=customer)}
                        ),
                    ),
                    ["c-a", "c-b"] * 6,
                )
            )
        for customer, result in results:
            assert result["rows"] == [
                {"n": 2, "total": 15} if customer == "c-a" else {"n": 1, "total": 7}
            ]
        spoofed = {**request, "policy_context": {"attributes": {"customer_id": "c-a"}}}
        with pytest.raises(SemanticLayerError) as caught:
            runtime.query(spoofed)
        assert caught.value.details["reason"] == "missing_attribute"
    finally:
        runtime.close()


class _Custom(WarehouseAdapter):
    engine = "custom"

    def query(self, sql, *, limits=None):
        raise AssertionError("parameterized SQL must never reach query(sql)")

    def close(self):
        pass


class _Legacy:
    engine = "legacy"
    supports_parameters = "yes"  # only True enables binding

    def query(self, sql):
        raise AssertionError("parameterized SQL must never reach query(sql)")


@pytest.mark.parametrize(
    "adapter_class",
    [
        AthenaAdapter,
        BigQueryNativeAdapter,
        ClickHouseAdapter,
        DatabricksNativeAdapter,
        DuckLakeAdapter,
        MotherDuckAdapter,
        PostgresAdapter,
        SnowflakeCliAdapter,
        SnowflakeNativeAdapter,
        _Custom,
        _Legacy,
    ],
)
def test_adapters_without_parameter_support_deny_before_any_driver_call(adapter_class):
    adapter = object.__new__(adapter_class)  # unconnected: denial precedes any driver access
    adapter.query = _forbidden
    calls = [
        lambda: _adapter_query(
            adapter, TOTALS, limits={}, policy_context=_context(customer_id="c-a")
        )
    ]
    if hasattr(adapter_class, "query_prepared"):
        calls.append(lambda: adapter_class.query_prepared(adapter, TOTALS, limits={}))
    for call in calls:
        with pytest.raises(SemanticLayerError) as caught:
            call()
        assert caught.value.code == "POLICY_DENIED"
        assert caught.value.details["reason"] == "parameters_unsupported"


def test_overrides_written_before_parameters_fail_closed(ledger):
    calls = []

    class ClaimsSupport(_Custom):
        supports_parameters = True  # but inherits the base query_prepared without parameters

    class WrappedDuckDB(DuckDBAdapter):
        def query(self, sql, *, limits=None):  # a host override that predates parameters
            calls.append(sql)
            return super().query(sql, limits=limits)

    wrapped = object.__new__(WrappedDuckDB)
    wrapped._db = ledger._db
    plain = PreparedQuery('SELECT COUNT(*) AS "n" FROM ledger')
    assert _adapter_query(wrapped, plain, limits={}) == [{"n": len(LEDGER)}]
    assert calls == [plain.sql]
    for adapter in (object.__new__(ClaimsSupport), wrapped):
        with pytest.raises(SemanticLayerError) as caught:  # the TypeError, unchained
            _adapter_query(adapter, TOTALS, limits={}, policy_context=_context(customer_id="c-a"))
        assert caught.value.code == "QUERY_EXECUTION_ERROR"
    assert calls == [plain.sql]
