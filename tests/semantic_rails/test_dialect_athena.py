"""Offline unit tests for the Athena (Trino SQL) connector.

No network: the PyAthena driver and boto3 are monkeypatched into
``sys.modules`` (mirroring how test_warehouse_adapters.py fakes
``snowflake.connector``), and the battery sweep only compiles SQL.
"""

from __future__ import annotations

import sys
import types
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.db import create_warehouse_adapter
from semantic_rails.db_parts.athena import AthenaAdapter, create_adapter
from semantic_rails.dialects import (
    AthenaDialect,
    dialect_for_warehouse,
    warehouse_connector,
)
from semantic_rails.errors import SemanticLayerError
from semantic_rails.renderer import render_expr, use_dialect
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ConnectionSpec, PackageMeta
from semantic_rails.sql_ast import SqlBinary, SqlIdentifier, SqlLiteral

DIALECT = AthenaDialect()


def _ident(*parts: str) -> SqlIdentifier:
    return SqlIdentifier(parts=list(parts))


# ---------------------------------------------------------------------------
# (a) Dialect rendering — every override, plus the inherited Trino-correct
#     defaults the connector relies on.
# ---------------------------------------------------------------------------


def _expected_exact_percentile(p: float, x: str = "x") -> str:
    # Exact linear interpolation over a sorted array (Trino arrays are
    # 1-based; COUNT skips NULLs and ASC sorts NULLS LAST, so positions
    # 1..n are exactly the sorted non-NULL values). Validated live on
    # Athena engine v3 against DuckDB QUANTILE_CONT (incl. NULL-mixed,
    # all-NULL, single-element, and p=0/p=1 inputs). Empty (or all-NULL)
    # input yields NULL through the CASE guard, not an arr[0] index error.
    pos = f"{p} * (COUNT({x}) - 1)"
    lo = f"(ARRAY_AGG({x} ORDER BY {x} ASC))[CAST(FLOOR({pos}) AS BIGINT) + 1]"
    hi = f"(ARRAY_AGG({x} ORDER BY {x} ASC))[CAST(CEIL({pos}) AS BIGINT) + 1]"
    return f"CASE WHEN COUNT({x}) > 0 THEN {lo} + ({hi} - {lo}) * ({pos} - FLOOR({pos})) END"


def test_athena_registry_entry_renders_trino_forms():
    connector = warehouse_connector("athena")
    assert connector is not None
    assert isinstance(connector.dialect, AthenaDialect)
    assert connector.connection_kinds == ("athena_native",)
    assert connector.adapter == "semantic_rails.db_parts.athena:create_adapter"
    assert isinstance(dialect_for_warehouse("athena"), AthenaDialect)

    condition = SqlBinary(_ident("x"), ">", SqlLiteral(1))
    null_safe = DIALECT.null_safe_eq(_ident("a"), _ident("b"))
    assert isinstance(null_safe, SqlBinary)
    rendered = [
        # The t-digest sketch (APPROX_PERCENTILE) is gone — exactness is the contract.
        (DIALECT.percentile_cont(_ident("x"), 0.95), _expected_exact_percentile(0.95)),
        (DIALECT.median(_ident("x")), _expected_exact_percentile(0.5)),
        (
            DIALECT.first_value(_ident("x"), _ident("t")),
            "MIN_BY(x, t) FILTER (WHERE x IS NOT NULL AND t IS NOT NULL)",
        ),
        (
            DIALECT.last_value(_ident("x"), _ident("t")),
            "MAX_BY(x, t) FILTER (WHERE x IS NOT NULL AND t IS NOT NULL)",
        ),
        # Trino: DATE_ADD('unit', value, ts) — NOT the INTERVAL form.
        (DIALECT.date_add("day", SqlLiteral(7), _ident("ts")), "DATE_ADD('day', 7, ts)"),
        (DIALECT.date_trunc("month", _ident("ts")), "DATE_TRUNC('month', CAST(ts AS TIMESTAMP))"),
        (null_safe, "a IS NOT DISTINCT FROM b"),
        (
            DIALECT.conditional_aggregate("count", condition, None),
            "COUNT(CASE WHEN x > 1 THEN 1 END)",
        ),
        (
            DIALECT.conditional_aggregate("sum", condition, _ident("v")),
            "SUM(CASE WHEN x > 1 THEN v END)",
        ),
    ]
    # Units the compiler emits (expressions.py window/offset units) stay lowercase; the
    # portable DATE_DIFF default IS Trino's form ('day', not 'DAY').
    for unit in ("minute", "hour", "day", "week", "month", "quarter", "year"):
        rendered.append(
            (DIALECT.date_add(unit, SqlLiteral(1), _ident("ts")), f"DATE_ADD('{unit}', 1, ts)")
        )
        rendered.append(
            (
                DIALECT.date_diff(unit, _ident("s"), _ident("e")),
                f"DATE_DIFF('{unit}', CAST(s AS TIMESTAMP), CAST(e AS TIMESTAMP))",
            )
        )
    for expr, expected in rendered:
        assert render_expr(expr) == expected
    assert DIALECT.timestamp_type_name() == "TIMESTAMP"

    capabilities = DIALECT.capabilities()
    assert capabilities["percentile_exact"] is True
    assert capabilities["median_exact"] is True
    assert "ARRAY_AGG" in capabilities["percentile_cont_method"]
    assert "percentile_approximation" not in capabilities
    assert capabilities["qualify"] is False
    assert capabilities["timestamp_type"] == "TIMESTAMP"


# ---------------------------------------------------------------------------
# (b) Adapter — fake pyathena driver, option/secret/env contracts,
#     error redaction.
# ---------------------------------------------------------------------------


class _FakeCursor:
    description = [("ONE",), ("TWO",)]

    def __init__(self, log: dict[str, Any], fail: Exception | None = None):
        self._log = log
        self._fail = fail

    def execute(self, sql: str) -> None:
        if self._fail is not None:
            raise self._fail
        self._log["sql"] = sql

    def fetchall(self):
        return [(1, "x")]

    def close(self) -> None:
        self._log["cursor_closed"] = True


class _FakeConnection:
    def __init__(self, log: dict[str, Any], fail: Exception | None = None):
        self._log = log
        self._fail = fail

    def cursor(self, **kwargs) -> _FakeCursor:
        self._log["cursor_kwargs"] = kwargs
        return _FakeCursor(self._log, self._fail)

    def close(self) -> None:
        self._log["connection_closed"] = True


def _install_fake_pyathena(
    monkeypatch: pytest.MonkeyPatch, log: dict[str, Any], fail: Exception | None = None
) -> None:
    module = types.ModuleType("pyathena")

    def _connect(**kwargs: Any) -> _FakeConnection:
        log["connect_kwargs"] = kwargs
        return _FakeConnection(log, fail)

    module.connect = _connect
    monkeypatch.setitem(sys.modules, "pyathena", module)
    botocore = types.ModuleType("botocore")
    config = types.ModuleType("botocore.config")

    class FakeConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.connect_timeout = kwargs["connect_timeout"]
            self.read_timeout = kwargs["read_timeout"]

    config.Config = FakeConfig
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", config)


def test_athena_adapter_normalizes_options_and_disables_statement_timeout():
    adapter = AthenaAdapter(
        {"Region-Env": " AWS_REGION ", "s3_staging_dir_env": "SR_ATHENA_S3_STAGING_DIR"}
    )
    assert adapter.options == {
        "region_env": "AWS_REGION",
        "s3_staging_dir_env": "SR_ATHENA_S3_STAGING_DIR",
    }
    assert adapter.supports_statement_timeout is False
    assert adapter.engine == "athena"
    assert adapter.connection_kind == "athena_native"

    package = PackageMeta(
        package_id="athena_demo",
        name="athena_demo",
        description="athena demo",
        warehouse="athena",
        connection=ConnectionSpec(
            kind="athena_native",
            options={
                "region_env": "AWS_REGION",
                "s3_staging_dir_env": "SR_ATHENA_S3_STAGING_DIR",
                "database": "sr_jaffle",
            },
        ),
    )
    adapter = create_warehouse_adapter(package)
    assert isinstance(adapter, AthenaAdapter)
    assert adapter.options["database"] == "sr_jaffle"
    assert create_adapter(package).options == adapter.options


def test_athena_adapter_rejects_unsupported_and_literal_secret_options():
    with pytest.raises(SemanticLayerError) as exc:
        AthenaAdapter({"host": "example.com"})
    assert exc.value.code == "INVALID_CONFIG"
    assert "unsupported package.connection option 'host'" in str(exc.value)

    # AWS credentials must come from the ambient boto3 chain — any
    # literal credential-looking option is rejected outright.
    for secret_key in ["password", "token", "secret", "api_key", "credentials", "private_key"]:
        with pytest.raises(SemanticLayerError) as exc:
            AthenaAdapter({secret_key: "hunter2"})
        assert exc.value.code == "INVALID_CONFIG"
        assert "hunter2" not in str(exc.value)


def test_athena_adapter_reports_missing_env_and_options_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
):
    log: dict[str, Any] = {}
    _install_fake_pyathena(monkeypatch, log)
    monkeypatch.delenv("SR_TEST_ATHENA_REGION", raising=False)
    monkeypatch.delenv("SR_TEST_ATHENA_STAGING", raising=False)
    adapter = AthenaAdapter(
        {
            "region_env": "SR_TEST_ATHENA_REGION",
            "s3_staging_dir_env": "SR_TEST_ATHENA_STAGING",
        }
    )

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("select 1")

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["missing_env"] == [
        "SR_TEST_ATHENA_REGION",
        "SR_TEST_ATHENA_STAGING",
    ]

    with pytest.raises(SemanticLayerError) as exc:
        AthenaAdapter({"database": "sr_jaffle"}).query("select 1")

    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details["missing_options"] == ["region", "s3_staging_dir"]
    assert "connect_kwargs" not in log


def test_athena_adapter_connects_with_env_indirection_and_defaults_namespace(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SR_TEST_ATHENA_REGION", "eu-central-1")
    monkeypatch.setenv("SR_TEST_ATHENA_STAGING", "s3://sr-bucket/athena-results/")
    from_env = {
        "region_env": "SR_TEST_ATHENA_REGION",
        "s3_staging_dir_env": "SR_TEST_ATHENA_STAGING",
        "database": "sr_jaffle",
        "workgroup": "primary",
    }
    # Namespace defaults from connection options so unqualified table names
    # (jaffle_order, …) resolve; without them the schema is "default" and no workgroup.
    env_kwargs = {
        "s3_staging_dir": "s3://sr-bucket/athena-results/",
        "region_name": "eu-central-1",
        "schema_name": "sr_jaffle",
        "work_group": "primary",
    }
    literal_kwargs = {
        "s3_staging_dir": "s3://sr-bucket/results",
        "region_name": "us-east-1",
        "schema_name": "default",
        "work_group": None,
    }
    for options, expected_kwargs, connect_timeout, read_timeout in [
        (from_env, env_kwargs, 10, 65),
        (
            {**from_env, "connect_timeout_seconds": "7", "read_timeout_seconds": "45"},
            env_kwargs,
            7,
            45,
        ),
        (
            {"region": "us-east-1", "s3_staging_dir": "s3://sr-bucket/results"},
            literal_kwargs,
            10,
            65,
        ),
    ]:
        log: dict[str, Any] = {}
        _install_fake_pyathena(monkeypatch, log)
        adapter = AthenaAdapter(options)

        rows = adapter.query("SELECT a / NULLIF(b, 0) FROM t WHERE ts >= '2016-09-01'")
        adapter.close()

        assert rows == [{"ONE": 1, "TWO": "x"}]
        # The ratio guard is prepared exactly once on the way to the driver.
        assert log["sql"] == (
            "SELECT a / CAST(NULLIF(b, 0) AS DOUBLE) FROM t WHERE ts >= '2016-09-01'"
        )
        connect_kwargs = log["connect_kwargs"]
        assert {
            key: value for key, value in connect_kwargs.items() if key not in {"config", "on_poll"}
        } == expected_kwargs
        assert connect_kwargs["config"].connect_timeout == connect_timeout
        assert connect_kwargs["config"].read_timeout == read_timeout
        assert "on_poll" not in connect_kwargs
        assert callable(log["cursor_kwargs"]["on_poll"])
        assert log["cursor_closed"] is True
        assert log["connection_closed"] is True


@pytest.mark.parametrize("timeout_ms, deadline", [(None, 3), (120000, 125)])
def test_athena_poll_deadline_cancels_before_abandoning_query(monkeypatch, timeout_ms, deadline):
    now = [0.0]
    monkeypatch.setattr("semantic_rails.db_parts.athena.time.monotonic", lambda: now[0])
    adapter = AthenaAdapter({"read_timeout_seconds": "3"})
    log = []
    cursor = types.SimpleNamespace(cancel=lambda: log.append("cancel"))
    callbacks = {}
    connection = types.SimpleNamespace(cursor=lambda **kwargs: callbacks.update(kwargs) or cursor)
    adapter._query_cursor(connection, (timeout_ms or 0) // 1000)
    execution = types.SimpleNamespace(query_id="query-1", state="RUNNING")
    now[0] = deadline - 0.1
    callbacks["on_poll"](execution)
    assert log == []
    now[0] = deadline
    with pytest.raises(TimeoutError, match="polling timed out"):
        callbacks["on_poll"](execution)
    assert log == ["cancel"]


def test_athena_installed_driver_calls_poll_hook_and_cancels(monkeypatch):
    import inspect

    pytest.importorskip("pyathena")
    import boto3
    from botocore.stub import ANY, Stubber
    from pyathena.connection import Connection

    assert "on_poll" in inspect.signature(Connection).parameters
    connection = Connection(
        s3_staging_dir="s3://example/results",
        region_name="us-east-1",
        poll_interval=0,
        session=boto3.Session(
            aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1"
        ),
    )
    now = [0.0]
    monkeypatch.setattr("semantic_rails.db_parts.athena.time.monotonic", lambda: now[0])
    adapter = AthenaAdapter({"read_timeout_seconds": "3"})
    cursor = adapter._query_cursor(connection, 0)
    original_get = cursor._get_query_execution

    def get_execution(query_id):
        execution = original_get(query_id)
        now[0] += 3
        return execution

    monkeypatch.setattr(cursor, "_get_query_execution", get_execution)
    with Stubber(connection.client) as stub:
        stub.add_response(
            "start_query_execution",
            {"QueryExecutionId": "query-1"},
            {
                "QueryString": ANY,
                "QueryExecutionContext": ANY,
                "ResultConfiguration": ANY,
            },
        )
        stub.add_response(
            "get_query_execution",
            {
                "QueryExecution": {
                    "QueryExecutionId": "query-1",
                    "Query": "select 1",
                    "Status": {"State": "RUNNING"},
                }
            },
            {"QueryExecutionId": "query-1"},
        )
        stub.add_response("stop_query_execution", {}, {"QueryExecutionId": "query-1"})
        with pytest.raises(TimeoutError, match="polling timed out"):
            cursor.execute("select 1")
        stub.assert_no_pending_responses()
    cursor.close()
    connection.close()


def test_athena_adapter_redacts_driver_errors(monkeypatch: pytest.MonkeyPatch):
    log: dict[str, Any] = {}
    _install_fake_pyathena(
        monkeypatch, log, fail=RuntimeError("SYNTAX_ERROR: line 1:8 mismatched input")
    )
    adapter = AthenaAdapter(
        {"region": "us-east-1", "s3_staging_dir": "s3://sr-bucket/results", "database": "db"}
    )

    with pytest.raises(SemanticLayerError) as exc:
        adapter.query("SELECT super_secret_column FROM jaffle_order")

    assert exc.value.code == "QUERY_EXECUTION_ERROR"
    details = exc.value.details
    assert details["engine"] == "athena"
    assert details["connection_kind"] == "athena_native"
    assert details["option_keys"] == ["database", "region", "s3_staging_dir"]
    assert details["sql_redacted"] is True
    # Never option VALUES, never the raw SQL.
    assert "sql" not in details
    assert "us-east-1" not in repr(details)
    assert "super_secret_column" not in repr(details)
    assert "SYNTAX_ERROR" not in str(exc.value)


# ---------------------------------------------------------------------------
# (b1) Compatibility pass — Trino integer/decimal division truncates, so
#      the compiler's `x / NULLIF(y, 0)` ratio guard is cast to DOUBLE
#      before execution (mirrors db_parts/postgres.py).
# ---------------------------------------------------------------------------


def test_athena_prepare_casts_nullif_ratio_guards_to_double_and_leaves_literals():
    for sql, expected in [
        (
            "SELECT a / NULLIF(b, 0) AS ratio FROM t",
            "SELECT a / CAST(NULLIF(b, 0) AS DOUBLE) AS ratio FROM t",
        ),
        (
            "SELECT x / NULLIF(y / NULLIF(z, 0), 0) FROM t",
            "SELECT x / CAST(NULLIF(y / CAST(NULLIF(z, 0) AS DOUBLE), 0) AS DOUBLE) FROM t",
        ),
        # Typing is the renderer's (SqlLiteral.temporal): a string compare stays one.
        (
            "SELECT 1 FROM t WHERE dt = '2026-01-01' AND ts >= '2016-09-01'",
            "SELECT 1 FROM t WHERE dt = '2026-01-01' AND ts >= '2016-09-01'",
        ),
    ]:
        assert DIALECT.prepare_query(sql).sql == expected


def test_athena_types_iso_temporal_literals_only():
    with use_dialect(DIALECT):
        for op, value, literal in [
            ("=", "2016-09-01", "TIMESTAMP '2016-09-01'"),
            ("<", "2016-09-01 12:30:00.123", "TIMESTAMP '2016-09-01 12:30:00.123'"),
            # Trino's TIMESTAMP literal takes a space between date and time, never ISO's T.
            (">", "2016-09-01T12:30:00", "TIMESTAMP '2016-09-01 12:30:00'"),
            ("<=", "2016-09-01T12:30:00.123456", "TIMESTAMP '2016-09-01 12:30:00.123456'"),
            (">=", "2016-09-01", "TIMESTAMP '2016-09-01'"),
            # Text that is not an ISO date or timestamp stays untyped.
            (">=", "2016-09", "'2016-09'"),
            (">=", "2016-09-011", "'2016-09-011'"),
            (">=", "last week", "'last week'"),
        ]:
            temporal = SqlBinary(_ident("t", "ts"), op, SqlLiteral(value, temporal=True))
            text = SqlBinary(_ident("t", "dt"), op, SqlLiteral(value))
            assert render_expr(temporal) == f"t.ts {op} {literal}"
            assert render_expr(text) == f"t.dt {op} '{value}'"


@pytest.fixture(scope="module")
def athena_runtime():
    from semantic_rails.config import load_package_config
    from tests.integration.harness import PACKAGE_DIR

    config = load_package_config(str(PACKAGE_DIR))
    runtime = Runtime.from_config(
        _athena_config(config), source_path=str(PACKAGE_DIR), package_id="jaffle_shop"
    )
    yield runtime
    runtime.close()


def test_athena_filters_and_now_type_only_date_and_timestamp_values(athena_runtime):
    sql = athena_runtime.compile(
        {
            "select": ["measure.jaffle.order_count"],
            "where": [
                {"field": "dimension.jaffle_store_name", "op": "is", "value": "2026-01-01"},
                {"field": "dimension.jaffle_order_ordered_at", "op": "gte", "value": "2016-09-01"},
                {
                    "field": "dimension.jaffle_order_ordered_at",
                    "op": "is",
                    "value": ["2016-09-01", "2016-09-02"],
                },
            ],
        },
        now="2026-10-01T00:00:00Z",
    )["rendered_sql"]
    assert "jaffle_store.store_name = '2026-01-01'" in sql
    assert ">= TIMESTAMP '2016-09-01'" in sql
    assert "IN (TIMESTAMP '2016-09-01', TIMESTAMP '2016-09-02')" in sql
    assert "TIMESTAMP '2026-01-01'" not in sql

    # A `this` window ends at `now`, an instant with a time of day.
    sql = athena_runtime.compile(
        {"select": ["measure.jaffle.order_count"], "time": {"this": "month", "grain": "day"}},
        now="2017-08-03T12:30:00Z",
    )["rendered_sql"]
    assert "< TIMESTAMP '2017-08-03 12:30:00'" in sql
    assert "TIMESTAMP '2017-08-03T" not in sql


def _order(column: str) -> dict[str, Any]:
    return {"kind": "column", "column": column, "entity": "entity.jaffle_order"}


def _call(name: str, *args: Any) -> dict[str, Any]:
    return {"kind": "call", "name": name, "args": list(args)}


_TEXT = {"kind": "literal", "value": "2016-09-01"}
_LATER = {"kind": "date_add", "unit": "day", "value": {"kind": "literal", "value": 1}, "date": _order("ordered_at")}  # fmt: skip
_IN_STORE_A = {"kind": "comparison", "op": "=", "left": _order("store_id"), "right": {"kind": "literal", "value": "a"}}  # fmt: skip


def _compares(left: dict[str, Any], op: str = ">=") -> dict[str, Any]:
    return {"kind": "comparison", "op": op, "left": left, "right": _TEXT}


# Text compared with a date or timestamp, a column or an expression whose result is one, is a
# TIMESTAMP literal (Trino refuses a timestamp compared with varchar); text compared with text
# stays text, whatever it looks like.
TEXT_COMPARISONS = [
    (_compares(_order("ordered_at")), "jaffle_order.ordered_at >= TIMESTAMP '2016-09-01'"),
    (
        {"kind": "comparison", "op": ">", "left": _TEXT, "right": _order("ordered_at")},
        "TIMESTAMP '2016-09-01' > jaffle_order.ordered_at",
    ),
    (
        {"kind": "in", "expr": _order("ordered_at"), "values": ["2016-09-01"]},
        "jaffle_order.ordered_at IN (TIMESTAMP '2016-09-01')",
    ),
    (
        {"kind": "in", "expr": _TEXT, "values": [_order("ordered_at")]},
        "TIMESTAMP '2016-09-01' IN (jaffle_order.ordered_at)",
    ),
    (
        _compares(_LATER),
        "DATE_ADD('day', 1, jaffle_order.ordered_at) >= TIMESTAMP '2016-09-01'",
    ),
    (
        _compares(_call("DATE_TRUNC", {"kind": "literal", "value": "month"}, _order("ordered_at"))),
        "DATE_TRUNC('month', jaffle_order.ordered_at) >= TIMESTAMP '2016-09-01'",
    ),
    (
        _compares(_call("COALESCE", _order("ordered_at"), _LATER)),
        ", 1, jaffle_order.ordered_at)) >= TIMESTAMP '2016-09-01'",
    ),
    (
        _compares(_call("NULLIF", _order("ordered_at"), _LATER)),
        ", 1, jaffle_order.ordered_at)) >= TIMESTAMP '2016-09-01'",
    ),
    (
        _compares(
            {
                "kind": "case",
                "whens": [{"when": _IN_STORE_A, "then": _order("ordered_at")}],
                "else": _LATER,
            }
        ),  # fmt: skip
        "END >= TIMESTAMP '2016-09-01'",
    ),
    # Text compared with text stays text, whatever it looks like.
    (_compares(_order("store_id"), "="), "jaffle_order.store_id = '2016-09-01'"),
    (
        _compares(_call("CAST", _order("ordered_at"), {"kind": "literal", "value": "VARCHAR"})),
        "CAST(jaffle_order.ordered_at AS VARCHAR) >= '2016-09-01'",
    ),
    (
        _compares(_call("COALESCE", _order("store_id"), {"kind": "literal", "value": "x"}), "="),
        "COALESCE(jaffle_order.store_id, 'x') = '2016-09-01'",
    ),
    (
        _compares(
            {
                "kind": "case",
                "whens": [{"when": _IN_STORE_A, "then": _order("store_id")}],
                "else": _TEXT,
            },
            "=",
        ),  # fmt: skip
        "END = '2016-09-01'",
    ),
]


def test_athena_expressions_type_text_compared_only_with_temporal_values(athena_runtime):
    aggregate = {"kind": "aggregate_if", "aggregation": "count", "value": _order("order_id")}
    for condition, rendered in TEXT_COMPARISONS:
        sql = athena_runtime.compile(
            {"select": [{"as": "n", "expression": {**aggregate, "condition": condition}}]},
            now="2026-10-01T00:00:00Z",
        )["rendered_sql"]
        assert rendered in sql
        assert ("TIMESTAMP '2016-09-01'" in sql) is ("TIMESTAMP" in rendered), rendered


def test_athena_relation_filters_type_text_compared_only_with_temporal_columns():
    """A relation's filter step reads its columns as the dimensions of what reads it declare
    them: text compared with a date or timestamp column is a TIMESTAMP literal, text compared
    with a text column stays text. A semi-join's filter reads the joined table's columns."""
    from pathlib import Path

    from semantic_rails.config import load_package_config
    from semantic_rails.relation_pipelines import lower_relation
    from semantic_rails.renderer import render_select
    from semantic_rails.schema import RelationConfig, RelationPipelineStep

    package = Path(__file__).parents[1] / "integration" / "correctness" / "pipelines"
    config = load_package_config(str(package))
    runtime = Runtime.from_config(
        _athena_config(config), source_path=str(package), package_id="pipelines"
    )
    try:
        sql = runtime.compile({"select": ["measure.pipelines.events"]})["rendered_sql"]
    finally:
        runtime.close()
    assert "src.day >= TIMESTAMP '2026-01-01'" in sql  # the {field, op, value} form
    assert "src.ts < TIMESTAMP '2026-02-01 00:00:00'" in sql  # the expression form
    assert "((src.label <> '2025-01-01') OR (src.label IS NULL))" in sql

    where = [
        {"field": "ordered_at", "op": "gte", "value": "2026-01-01"},
        {"field": "note", "op": "is", "value": "2026-01-01"},
    ]
    semi_join = {"relation": "orders", "on": [{"left": "id", "right": "id"}], "where": where}
    relation = RelationConfig(
        id="relation.t.kept",
        output_name="kept",
        steps=[
            RelationPipelineStep("source", {"relation": "events", "columns": ["id"]}),
            RelationPipelineStep("semi_join", semi_join),
        ],
    )
    ctes, _ = lower_relation(
        relation, warehouse="athena", tables={"orders": frozenset({"ordered_at"})}
    )
    with use_dialect(DIALECT):
        rendered = "\n".join(render_select(cte.query) for cte in ctes)
    assert "\"right\".ordered_at >= TIMESTAMP '2026-01-01'" in rendered
    assert "\"right\".note = '2026-01-01'" in rendered


# ---------------------------------------------------------------------------
# (b2) Loader — fake adapter + fake boto3; verify upload + Hive DDL.
# ---------------------------------------------------------------------------


class _FakeLoaderAdapter:
    engine = "athena"
    connection_kind = "athena_native"

    def __init__(self, options: dict[str, str], marker_rows: list[dict[str, Any]] | None = None):
        self.options = options
        self.executed: list[str] = []
        self._marker_rows = marker_rows

    def query(self, sql: str, *, limits: dict[str, Any] | None = None):
        self.executed.append(sql)
        if sql.strip().upper().startswith("SELECT FINGERPRINT"):
            if self._marker_rows is None:
                raise RuntimeError("marker table missing")
            return self._marker_rows
        return []

    def close(self) -> None:
        return None


def _install_fake_boto3(monkeypatch: pytest.MonkeyPatch, uploads: list[tuple[str, str, str]]):
    module = types.ModuleType("boto3")

    class _FakeS3Client:
        def __init__(self, region_name: str = ""):
            self.region_name = region_name

        def upload_file(self, filename: str, bucket: str, key: str) -> None:
            uploads.append((filename, bucket, key))

    def _client(service: str, region_name: str = "", **kwargs: Any) -> _FakeS3Client:
        assert service == "s3"
        module.last_region = region_name
        return _FakeS3Client(region_name)

    module.client = _client
    monkeypatch.setitem(sys.modules, "boto3", module)
    return module


def _fake_fixture(tmp_path):
    from tests.integration.fixture import FixtureColumn, FixtureTable, JaffleFixture

    table = FixtureTable(
        name="jaffle_order",
        columns=(
            FixtureColumn(name="order_id", duckdb_type="BIGINT", logical_type="integer"),
            FixtureColumn(name="amount", duckdb_type="DECIMAL(18,3)", logical_type="decimal"),
            FixtureColumn(name="ratio", duckdb_type="DOUBLE", logical_type="float"),
            FixtureColumn(name="store", duckdb_type="VARCHAR", logical_type="string"),
            FixtureColumn(name="day", duckdb_type="DATE", logical_type="date"),
            FixtureColumn(name="at", duckdb_type="TIMESTAMP", logical_type="timestamp"),
            FixtureColumn(name="ok", duckdb_type="BOOLEAN", logical_type="boolean"),
        ),
        row_count=3,
    )
    (tmp_path / "jaffle_order.parquet").write_bytes(b"PAR1fake")
    return JaffleFixture(path=tmp_path, fingerprint="fp123", tables=(table,))


def test_athena_loader_uploads_parquet_and_creates_external_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    from tests.integration.loaders.athena import AthenaFixtureLoader

    uploads: list[tuple[str, str, str]] = []
    boto3_module = _install_fake_boto3(monkeypatch, uploads)
    monkeypatch.setenv("SR_TEST_ATHENA_STAGING", "s3://sr-bucket/athena/results/")
    adapter = _FakeLoaderAdapter(
        {
            "region": "us-east-1",
            "s3_staging_dir_env": "SR_TEST_ATHENA_STAGING",
            "database": "sr_jaffle",
        }
    )
    fixture = _fake_fixture(tmp_path)
    loader = AthenaFixtureLoader(adapter)

    loader.ensure_loaded(fixture)

    assert uploads == [
        (
            str(tmp_path / "jaffle_order.parquet"),
            "sr-bucket",
            "athena/results/sr_fixture/fp123/jaffle_order/part.parquet",
        )
    ]
    assert boto3_module.last_region == "us-east-1"
    assert "CREATE DATABASE IF NOT EXISTS sr_jaffle" in adapter.executed
    assert "DROP TABLE IF EXISTS jaffle_order" in adapter.executed
    create_sql = next(sql for sql in adapter.executed if sql.startswith("CREATE EXTERNAL TABLE"))
    assert create_sql == (
        "CREATE EXTERNAL TABLE jaffle_order ("
        "order_id bigint, amount decimal(18,3), ratio double, store string, "
        "day date, at timestamp, ok boolean) "
        "STORED AS PARQUET "
        "LOCATION 's3://sr-bucket/athena/results/sr_fixture/fp123/jaffle_order/'"
    )
    # Marker is a CTAS — Athena has no plain CREATE TABLE + INSERT.
    assert "DROP TABLE IF EXISTS sr_fixture_meta" in adapter.executed
    assert "CREATE TABLE sr_fixture_meta AS SELECT 'fp123' AS fingerprint" in adapter.executed
    assert not any(sql.lstrip().upper().startswith("INSERT") for sql in adapter.executed)


def test_athena_loader_skips_load_when_marker_matches(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from tests.integration.loaders.athena import AthenaFixtureLoader

    uploads: list[tuple[str, str, str]] = []
    _install_fake_boto3(monkeypatch, uploads)
    adapter = _FakeLoaderAdapter(
        {"region": "us-east-1", "s3_staging_dir": "s3://sr-bucket/results"},
        marker_rows=[{"fingerprint": "fp123"}],
    )
    fixture = _fake_fixture(tmp_path)

    AthenaFixtureLoader(adapter).ensure_loaded(fixture)

    assert uploads == []
    assert adapter.executed == ["SELECT fingerprint FROM sr_fixture_meta"]


def test_athena_target_uses_env_indirection_and_ambient_aws_creds():
    from tests.integration.targets.athena import TARGET

    assert TARGET.warehouse == "athena"
    assert TARGET.connection_kind == "athena_native"
    assert TARGET.connection_options["region_env"] == "AWS_REGION"
    assert TARGET.connection_options["s3_staging_dir_env"] == "SR_ATHENA_S3_STAGING_DIR"
    assert TARGET.connection_options["database"]
    # Credentials are ambient (boto3 chain) — required env, never options.
    for name in (
        "AWS_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "SR_ATHENA_S3_STAGING_DIR",
        "SR_ATHENA_DATABASE",
    ):
        assert name in TARGET.required_env
    assert "AWS_ACCESS_KEY_ID" not in str(TARGET.connection_options)
    assert TARGET.make_loader is not None


# ---------------------------------------------------------------------------
# (c) Compile-only sweep — the full battery compiles for warehouse=athena
#     with non-empty, Trino-safe SQL. No warehouse connection involved.
# ---------------------------------------------------------------------------


def _athena_config(config):
    return replace(
        config,
        package=replace(
            config.package,
            warehouse="athena",
            default_db="",
            seed=replace(config.package.seed, kind="", source="", post_sql=""),
            connection=ConnectionSpec(
                kind="athena_native",
                options={
                    "region_env": "AWS_REGION",
                    "s3_staging_dir_env": "SR_ATHENA_S3_STAGING_DIR",
                    "database": "sr_jaffle",
                },
            ),
        ),
    )


def test_athena_compiles_entire_battery_to_non_empty_sql(athena_runtime):
    from tests.integration.harness import load_battery

    cases = load_battery()
    assert cases, "battery is empty — harness misconfigured"
    for case in cases:
        compiled = athena_runtime.compile(case.payload)
        sql = str(compiled.get("rendered_sql") or "")
        assert sql.strip(), f"{case.name}: compiled to empty rendered SQL"
        # DuckDB-only forms must never leak into Athena SQL, and the
        # approximate percentile sketch is retired in favor of the
        # exact ARRAY_AGG interpolation.
        for forbidden in (
            "QUANTILE_CONT(",
            "MEDIAN(",
            "arg_min(",
            "arg_max(",
            "APPROX_PERCENTILE(",
        ):
            assert forbidden not in sql, f"{case.name}: forbidden {forbidden} in SQL"
        # Trino rejects the portable INTERVAL date_add form.
        assert "INTERVAL (" not in sql, f"{case.name}: INTERVAL form leaked into SQL"
