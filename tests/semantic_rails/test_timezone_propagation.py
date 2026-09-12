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

from pathlib import Path

import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.registry import Registry


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
