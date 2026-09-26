"""Verify that ``times.<key>.column_timezone`` propagates into emitted SQL.

The temporal role's ``column_timezone`` (source zone) and ``timezone``
(target zone) should produce a timezone-conversion wrap around the raw
time-column expression — applied **before** any ``DATE_TRUNC`` or grain
bucketing — so truncation and filtering happen in the target zone.

The spelling of that wrap is per-dialect: ``CONVERT_TIMEZONE`` exists
only on Snowflake and Databricks, so DuckDB/Postgres, BigQuery,
ClickHouse and Trino/Athena each emit their own form.

When the two zones are absent or equal, no wrap is emitted.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.db import DuckDBAdapter, WarehouseAdapter
from semantic_rails.db_parts.postgres import PostgresAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime


def _query() -> dict[str, object]:
    return {
        "select": [
            {
                "expression": {
                    "measure": "measure.jaffle.order_count",
                    "aggregation": "count_distinct",
                },
                "as": "orders",
            },
        ],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "month",
        },
    }


def _patch_orders_times(
    package_path: Path,
    *,
    timezone: str | None,
    column_timezone: str | None,
) -> None:
    """Inject (or clear) timezone / column_timezone on the orders.ordered_at
    times block in the copied jaffle_shop package."""
    # ``package_path`` from ``package_config_factory`` is either the package
    # directory itself (jaffle_shop is a directory package) or a single YAML
    # file. Handle both.
    if package_path.is_dir():
        orders_yml = package_path / "models" / "core" / "orders.yml"
    else:
        orders_yml = package_path.parent / "models" / "core" / "orders.yml"
    raw = dict(yaml.safe_load(orders_yml.read_text(encoding="utf-8")) or {})
    model = dict(raw.get("model", {}) or {})
    times = dict(model.get("times", {}) or {})
    ordered_at = dict(times.get("ordered_at", {}) or {})
    if timezone is None:
        ordered_at.pop("timezone", None)
    else:
        ordered_at["timezone"] = timezone
    if column_timezone is None:
        ordered_at.pop("column_timezone", None)
    else:
        ordered_at["column_timezone"] = column_timezone
    times["ordered_at"] = ordered_at
    model["times"] = times
    raw["model"] = model
    orders_yml.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def test_column_timezone_wraps_raw_expr_with_convert_timezone(
    package_config_factory,
) -> None:
    """jaffle_shop is a DuckDB package, so the emitted rewrite must be
    DuckDB's. This previously asserted Snowflake's CONVERT_TIMEZONE — which
    the base dialect emitted for all nine warehouses, and which DuckDB
    rejects at execute time with `Scalar Function with name
    convert_timezone does not exist!`."""
    _, package_path = package_config_factory("jaffle_shop")
    _patch_orders_times(
        package_path,
        timezone="America/New_York",
        column_timezone="UTC",
    )
    config = load_package_config(str(package_path))  # reload after YAML mutation

    compiled = compile_query(config, Registry(config), _query())
    rendered = compiled["sql"]

    assert "TIMEZONE('America/New_York', TIMEZONE('UTC'" in rendered, rendered
    assert "CONVERT_TIMEZONE" not in rendered, rendered
    # The wrap goes inside DATE_TRUNC — truncation happens in the target zone.
    assert "DATE_TRUNC('month', CAST(TIMEZONE(" in rendered, rendered


def test_column_timezone_rewrite_actually_runs_on_duckdb(package_config_factory) -> None:
    """Compile-time success meant nothing before: the old rewrite produced
    ok:true and then died at execute time on the reference warehouse."""
    import duckdb

    _, package_path = package_config_factory("jaffle_shop")
    _patch_orders_times(
        package_path,
        timezone="America/New_York",
        column_timezone="UTC",
    )
    config = load_package_config(str(package_path))
    rendered = compile_query(config, Registry(config), _query())["sql"]

    con = duckdb.connect()
    con.execute("CREATE TABLE jaffle_order (order_id VARCHAR, ordered_at TIMESTAMP)")
    con.execute("INSERT INTO jaffle_order VALUES ('o1', TIMESTAMP '2024-01-15 12:00:00')")
    rows = con.execute(rendered).fetchall()
    assert rows, rendered
    # 12:00 UTC is 07:00 in New York, so the row truncates into January.
    assert str(rows[0][0]).startswith("2024-01-01")


@pytest.mark.parametrize(
    ("warehouse", "expected"),
    [
        ("duckdb", "TIMEZONE('America/New_York', TIMEZONE('UTC', t.ts))"),
        ("motherduck", "TIMEZONE('America/New_York', TIMEZONE('UTC', t.ts))"),
        ("ducklake", "TIMEZONE('America/New_York', TIMEZONE('UTC', t.ts))"),
        ("postgres", "TIMEZONE('America/New_York', TIMEZONE('UTC', t.ts))"),
        ("snowflake", "CONVERT_TIMEZONE('UTC', 'America/New_York', t.ts)"),
        ("databricks", "CONVERT_TIMEZONE('UTC', 'America/New_York', t.ts)"),
        ("bigquery", "DATETIME(TIMESTAMP(t.ts, 'UTC'), 'America/New_York')"),
        ("clickhouse", "toTimeZone(toDateTime(t.ts, 'UTC'), 'America/New_York')"),
        ("athena", "AT_TIMEZONE(WITH_TIMEZONE(t.ts, 'UTC'), 'America/New_York')"),
    ],
)
def test_every_warehouse_emits_its_own_timezone_rewrite(warehouse: str, expected: str) -> None:
    from semantic_rails.dialects import dialect_for_warehouse
    from semantic_rails.renderer import render_expr
    from semantic_rails.sql_ast import SqlIdentifier

    dialect = dialect_for_warehouse(warehouse)
    rendered = render_expr(
        dialect.convert_timezone("UTC", "America/New_York", SqlIdentifier(parts=["t", "ts"]))
    )
    assert rendered == expected


def test_unknown_warehouse_refuses_the_rewrite_instead_of_guessing() -> None:
    """The base class used to emit Snowflake syntax for anything it did not
    recognize. Refusing to compile is the honest outcome."""
    from semantic_rails.dialects import SqlDialect
    from semantic_rails.errors import SemanticLayerError
    from semantic_rails.sql_ast import SqlIdentifier

    with pytest.raises(SemanticLayerError) as exc:
        SqlDialect(name="generic").convert_timezone(
            "UTC", "America/New_York", SqlIdentifier(parts=["t", "ts"])
        )

    assert exc.value.code == "REWRITE_NOT_SUPPORTED"
    assert exc.value.details["rewrite"] == "convert_timezone"


def test_no_column_timezone_emits_no_convert_timezone(
    package_config_factory,
) -> None:
    _, package_path = package_config_factory("jaffle_shop")
    # Leave column_timezone empty; timezone may default to UTC.
    _patch_orders_times(
        package_path,
        timezone="UTC",
        column_timezone=None,
    )
    config = load_package_config(str(package_path))  # reload after YAML mutation

    compiled = compile_query(config, Registry(config), _query())
    rendered = compiled["sql"]

    assert "CONVERT_TIMEZONE" not in rendered, rendered


def test_column_timezone_equal_to_timezone_emits_no_convert_timezone(
    package_config_factory,
) -> None:
    _, package_path = package_config_factory("jaffle_shop")
    _patch_orders_times(
        package_path,
        timezone="UTC",
        column_timezone="UTC",
    )
    config = load_package_config(str(package_path))  # reload after YAML mutation

    compiled = compile_query(config, Registry(config), _query())
    rendered = compiled["sql"]

    assert "CONVERT_TIMEZONE" not in rendered, rendered


# -- zone-aware columns: answers in the role's zone, whatever the session's ------------

# Four instants and their day in each zone (UTC / New York / Los Angeles):
# 02:00 UTC Dec 1 is Nov 30 in both US zones, 06:00 UTC Dec 1 is Nov 30 only in Los
# Angeles, and 04:30 UTC Dec 2 is still Dec 1 in both.
ZONE_SEED = """
CREATE TABLE orders (order_id INTEGER, ordered_at TIMESTAMP, ordered_at_tz TIMESTAMPTZ,
  amount DOUBLE);
INSERT INTO orders VALUES
  (1, TIMESTAMP '2023-12-01 02:00:00', TIMESTAMPTZ '2023-12-01 02:00:00+00', 1),
  (2, TIMESTAMP '2023-12-01 06:00:00', TIMESTAMPTZ '2023-12-01 06:00:00+00', 2),
  (3, TIMESTAMP '2023-12-01 23:30:00', TIMESTAMPTZ '2023-12-01 23:30:00+00', 4),
  (4, TIMESTAMP '2023-12-02 04:30:00', TIMESTAMPTZ '2023-12-02 04:30:00+00', 8);
"""
ZONE_ORDERS = """
model:
  id: orders
  relation: orders
  entities: {order: {}}
  times:
    ordered_at: {column: %s, kind: timestamp, class: event_time, default: true%s}
  measures:
    revenue: {kind: aggregate, expr: amount, accumulation: {kind: flow}}
"""


ZONE_ROLE = "temporal_role.zones_order_ordered_at"
ZONE_REVENUE = [{"expression": {"measure": "measure.zones.revenue"}, "as": "revenue"}]


def _zone_runtime(root: Path, column: str, zones: str) -> Runtime:
    package = root / "zones"
    files = {
        "package.yml": "schema_version: 1\npackage: {id: zones, namespace: zones, warehouse: duckdb,"
        " default_db: data/warehouse.duckdb, seed: {kind: sql_script, source: data/seed.sql}}\n",
        "data/seed.sql": ZONE_SEED,
        "models/orders.yml": ZONE_ORDERS % (column, zones),
        "graph.yml": "graph: {entities: {order: {key: [order_id], model: orders}}}\n",
    }
    for name, text in files.items():
        (package / name).parent.mkdir(parents=True, exist_ok=True)
        (package / name).write_text(text, encoding="utf-8")
    runtime = Runtime.from_path(str(package))
    # Every connection to this database now defaults to Los Angeles, the way a laptop
    # or a server configured for that zone would.
    runtime._get_adapter()._db.conn.execute("SET GLOBAL TimeZone = 'America/Los_Angeles'")  # noqa: SLF001
    return runtime


@pytest.mark.parametrize(
    ("column", "zones", "days", "months"),
    [
        # A zone-aware column is bucketed and bounded in the role's zone (UTC by default).
        ("ordered_at_tz", "", {"2023-12-01": 7}, {"2023-12-01": 15}),
        (
            "ordered_at_tz",
            ", timezone: America/New_York",
            {"2023-12-01": 14},
            {"2023-11-01": 1, "2023-12-01": 14},
        ),
        # Naive columns keep their stored clock, converted only by column_timezone.
        ("ordered_at", "", {"2023-12-01": 7}, {"2023-12-01": 15}),
        (
            "ordered_at",
            ", timezone: America/New_York, column_timezone: UTC",
            {"2023-12-01": 14},
            {"2023-11-01": 1, "2023-12-01": 14},
        ),
    ],
)
def test_answers_do_not_follow_the_session_time_zone(
    tmp_path: Path, column: str, zones: str, days: dict[str, int], months: dict[str, int]
) -> None:
    runtime = _zone_runtime(tmp_path, column, zones)
    try:
        answers = []
        for time in (
            {
                "temporal_role": ZONE_ROLE,
                "grain": "day",
                "start": "2023-12-01",
                "end": "2023-12-02",
            },
            {"temporal_role": ZONE_ROLE, "grain": "month"},
        ):
            result = runtime.query({"version": 1, "select": ZONE_REVENUE, "time": time})
            answers.append(
                {
                    str(row[f"{ZONE_ROLE}__{time['grain']}"])[:10]: row["revenue"]
                    for row in result["rows"]
                }
            )
        # The zone ended with each query's cursor: the connection the host sees, and any
        # cursor made from it later, still read Los Angeles.
        conn = runtime._get_adapter()._db.conn  # noqa: SLF001
        zone = "SELECT current_setting('TimeZone')"
        assert conn.execute(zone).fetchone() == conn.cursor().execute(zone).fetchone()
        assert conn.execute(zone).fetchone() == ("America/Los_Angeles",)
    finally:
        runtime.close()
    assert answers == [days, months], result["rendered_sql"]


class _CapturingAdapter(WarehouseAdapter):
    """A host's own adapter: it gets the zone as ``limits["time_zone"]`` and nothing else."""

    engine = "duckdb"

    def __init__(self) -> None:
        self.limits: list[dict[str, Any]] = []

    def query(self, sql: str, *, limits: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.limits.append(dict(limits or {}))
        return []

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("zones", "expected"),
    [("", "UTC"), (", timezone: America/New_York", "America/New_York"), (", timezone: Mars", "")],
)
def test_an_injected_adapter_receives_the_role_zone(
    tmp_path: Path, zones: str, expected: str
) -> None:
    runtime = _zone_runtime(tmp_path, "ordered_at_tz", zones)
    adapter = _CapturingAdapter()
    runtime.set_adapter(adapter)
    time = {"temporal_role": ZONE_ROLE, "grain": "day", "start": "2023-12-01", "end": "2023-12-02"}
    try:
        runtime.query({"version": 1, "select": ZONE_REVENUE, "time": time})
    finally:
        runtime.close()
    assert adapter.limits
    assert {limits.get("time_zone") for limits in adapter.limits} == {expected}


def test_a_database_from_before_time_zone_still_runs() -> None:
    """Only a ``Database.query`` that takes ``time_zone=`` is given it."""
    calls: list[dict[str, Any]] = []

    class OlderDatabase:
        def query(self, sql: str, params: Any = None, *, max_rows: Any = None) -> list:
            calls.append({"sql": sql, "max_rows": max_rows})
            return [{"n": 1}]

    adapter = DuckDBAdapter.__new__(DuckDBAdapter)
    adapter._db = OlderDatabase()  # noqa: SLF001
    assert adapter.query("SELECT 1 AS n", limits={"time_zone": "UTC"}) == [{"n": 1}]
    assert calls == [{"sql": "SELECT 1 AS n", "max_rows": None}]


def _postgres_with(connection: Any) -> PostgresAdapter:
    options = {"host_env": "SR_POSTGRES_HOST", "user_env": "SR_POSTGRES_USER"}
    options |= {"password_env": "SR_POSTGRES_PASSWORD"}
    options["port"] = os.environ.get("SR_POSTGRES_PORT", "5432")
    options["database"] = os.environ.get("SR_POSTGRES_DATABASE", "postgres")

    class HostOwned(PostgresAdapter):
        def _create_connection(self) -> Any:
            return connection if connection is not None else super()._create_connection()

    return HostOwned(options)


@pytest.mark.parametrize(
    ("status", "current", "expected"),
    [
        # The engine's own idle connection: SET LOCAL in a transaction of its own.
        ("IDLE", "America/Los_Angeles", ["BEGIN", "UTC", "SQL", "COMMIT"]),
        # A host's open transaction: its zone is put back before the transaction goes on.
        (
            "INTRANS",
            "America/Los_Angeles",
            ["BEGIN", "UTC", "SQL", "America/Los_Angeles", "COMMIT"],
        ),
        # Already in the zone, under any of its names: no transaction and no SET.
        ("IDLE", "UTC", ["SQL"]),
        ("IDLE", "Etc/UTC", ["SQL"]),
    ],
)
def test_postgres_scopes_the_zone_to_the_query(status: str, current: str, expected: list) -> None:
    log: list[str] = []

    class Cursor:
        description = [("n",)]

        def execute(self, sql: str, params: tuple[str, ...] = ()) -> None:
            log.append(params[0] if "set_config" in sql else "SQL")

        def fetchall(self) -> list[tuple[int]]:
            return [(1,)]

        def close(self) -> None:
            pass

    class Connection:
        info = SimpleNamespace(
            transaction_status=SimpleNamespace(name=status),
            parameter_status=lambda name: current,
        )

        def cursor(self) -> Cursor:
            return Cursor()

        @contextmanager
        def transaction(self) -> Iterator[None]:
            log.append("BEGIN")
            yield
            log.append("COMMIT")

    rows = _postgres_with(Connection()).query("SELECT 1 AS n", limits={"time_zone": "UTC"})
    assert rows == [{"n": 1}]
    assert log == expected


@pytest.mark.skipif(
    not os.environ.get("SR_POSTGRES_HOST"), reason="needs a Postgres server (SR_POSTGRES_*)"
)
def test_postgres_zone_never_outlives_the_query() -> None:
    psycopg = pytest.importorskip("psycopg")
    zone_sql = "SELECT current_setting('TimeZone') AS zone"
    adapter = _postgres_with(None)
    try:
        adapter.query("SET TimeZone = 'America/Los_Angeles'")
        assert adapter.query(zone_sql, limits={"time_zone": "Asia/Tokyo"}) == [
            {"zone": "Asia/Tokyo"}
        ]
        assert adapter.query(zone_sql) == [{"zone": "America/Los_Angeles"}]
        # The same with a host's connection, inside a transaction the host opened.
        host = psycopg.connect(**adapter._connect_kwargs() | {"autocommit": False})  # noqa: SLF001
        host.execute("SET LOCAL TimeZone = 'Europe/Paris'")
        hosted = _postgres_with(host)
        assert hosted.query(zone_sql, limits={"time_zone": "Asia/Tokyo"}) == [
            {"zone": "Asia/Tokyo"}
        ]
        assert host.execute(zone_sql).fetchone() == ("Europe/Paris",)
        # A failed statement rolls back to the scope's savepoint, zone included.
        with pytest.raises(SemanticLayerError):
            hosted.query("SELECT 1 / 0", limits={"time_zone": "Asia/Tokyo"})
        assert host.execute(zone_sql).fetchone() == ("Europe/Paris",)
        server_zone = adapter.query("SELECT reset_val FROM pg_settings WHERE name = 'TimeZone'")
        host.commit()
        assert host.execute(zone_sql).fetchone() == (server_zone[0]["reset_val"],)
        hosted.close()
    finally:
        adapter.close()
