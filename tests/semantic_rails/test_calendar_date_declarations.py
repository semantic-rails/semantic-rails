"""Authored calendar anchors must declare dates before a query executes."""

from dataclasses import replace
from datetime import date, datetime
from unittest.mock import patch

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.compiler_parts.sql_lowering import (
    _calendar_fill_binding,
    _leaf_calendar_binding,
)
from semantic_rails.config import (
    _load_package_source,
    _parse_package,
    load_package_config,
    resolve_repo_path,
)
from semantic_rails.config_parts.package_loader import normalize_package
from semantic_rails.db import Database, DuckDBAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime

ROLE = "temporal_role.jaffle_order_time"
EXPECTED = [(date(2024, 5, 6), 2), (date(2024, 5, 13), 0)]
REFERENCE = """
    SELECT bucket, COUNT(DISTINCT o.order_id) AS orders
    FROM (VALUES (DATE '2024-05-06'), (DATE '2024-05-13')) AS weeks(bucket)
    LEFT JOIN jaffle_order AS o
      ON o.ordered_at >= bucket AND o.ordered_at < bucket + INTERVAL '7 days'
    GROUP BY bucket ORDER BY bucket
"""


def _query(calendar_id="default"):
    return {
        "version": 1,
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
        "time": {
            "temporal_role": ROLE,
            "grain": "week",
            "start": "2024-05-06",
            "end": "2024-05-20",
            "fill": True,
            "calendar_id": calendar_id,
        },
        "order_by": [{"field": f"{ROLE}__week"}],
    }


def _runtime(
    calendar_id="default", column="week_start", declared_type="date", *, missing_day=False
):
    source = resolve_repo_path("configs/semantic_rails/jaffle_shop")
    if missing_day:
        raw = _load_package_source(source)
        model = raw["graph"]["entities"]["fiscal_calendar"]["model"]
        raw["graph"]["entities"]["fiscal_calendar"]["key"] = ["date_id"]
        del raw["models"][model]["times"]["date_day"]
        config = _parse_package(normalize_package(raw), path=source)
    else:
        config = load_package_config(source)
    calendar = next(
        row for row in config.entities if row.kind == "time" and row.calendar_id == calendar_id
    )
    config = replace(
        config,
        aggregate_relations=[],
        dimensions=[
            replace(row, data_type=declared_type)
            if row.entity == calendar.id and row.column == column
            else row
            for row in config.dimensions
        ],
    )
    adapter = DuckDBAdapter.__new__(DuckDBAdapter)
    adapter._db = Database.connect_in_memory()
    adapter._db.conn.execute("""
        CREATE TABLE jaffle_order AS SELECT * FROM (VALUES
          ('first', TIMESTAMP '2024-05-06 09:00:00'),
          ('second', TIMESTAMP '2024-05-07 12:00:00'),
          ('later', TIMESTAMP '2024-05-25 12:00:00')
        ) AS orders(order_id, ordered_at);
        CREATE TABLE jaffle_calendar AS SELECT d::DATE AS date_day,
          date_trunc('week', d)::DATE AS week_start,
          date_trunc('month', d)::DATE AS month_start,
          date_trunc('quarter', d)::DATE AS quarter_start,
          date_trunc('year', d)::DATE AS year_start
        FROM range(DATE '2024-05-01', DATE '2024-06-01', INTERVAL 1 DAY) AS days(d);
        CREATE TABLE jaffle_calendar_fiscal AS SELECT * FROM jaffle_calendar;
    """)
    if missing_day:
        adapter._db.conn.execute(f"ALTER TABLE {calendar.table} ADD COLUMN date_id BIGINT")
        adapter._db.conn.execute(
            f"UPDATE {calendar.table} SET date_id = date_diff('day', DATE '2024-05-01', date_day)"
        )
        adapter._db.conn.execute(
            f"ALTER TABLE {calendar.table} ALTER COLUMN date_day SET DATA TYPE TIMESTAMP"
        )
        adapter._db.conn.execute(
            f"UPDATE {calendar.table} SET date_day = date_day + INTERVAL '12 hours'"
        )
    if declared_type == "timestamp":
        adapter._db.conn.execute(
            f"ALTER TABLE {calendar.table} ALTER COLUMN {column} SET DATA TYPE TIMESTAMP"
        )
        adapter._db.conn.execute(
            f"UPDATE {calendar.table} SET {column} = {column} + INTERVAL '12 hours'"
        )
    runtime = Runtime.from_config(config, source_path=source)
    runtime.set_aggregate_routing(False)
    runtime.set_adapter(adapter)
    return runtime


@pytest.mark.parametrize(
    ("calendar_id", "column", "declared_type"),
    [
        ("default", "week_start", "timestamp"),
        ("default", "date_day", "timestamp"),
        ("fiscal", "week_start", "timestamp"),
        ("fiscal", "date_day", "timestamp"),
        ("default", "week_start", "integer"),
        ("fiscal", "week_start", "categorical"),
    ],
)
def test_calendar_anchor_refuses_before_execution(calendar_id, column, declared_type):
    runtime = _runtime(calendar_id, column, declared_type)
    try:
        assert runtime.adapter._db.conn.execute(REFERENCE).fetchall() == EXPECTED
        with patch.object(runtime.adapter, "query", wraps=runtime.adapter.query) as execute:
            with pytest.raises(SemanticLayerError) as refused:
                runtime.query(_query(calendar_id))
            execute.assert_not_called()
        assert refused.value.code == "REWRITE_NOT_SUPPORTED"
        assert (
            refused.value.details.items()
            >= {
                "calendar_id": calendar_id,
                "column": column,
                "declared_type": declared_type,
            }.items()
        )
        assert f"declare `{column}` as a date" in str(refused.value)
    finally:
        runtime.close()


def test_unchanged_shop_calendar_matches_reference():
    runtime = _runtime()
    try:
        reference = runtime.adapter._db.conn.execute(REFERENCE).fetchall()
        result = runtime.query(_query())
        assert result["status"] == "ok"
        assert [
            (datetime.fromisoformat(row[f"{ROLE}__week"]).date(), row["orders"])
            for row in result["rows"]
        ] == reference
        assert reference == EXPECTED
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["runtime", "leaf_binding"])
def test_cross_calendar_join_refuses_an_undeclared_day(path):
    runtime = _runtime("fiscal", missing_day=True)
    try:
        assert runtime.adapter._db.conn.execute(REFERENCE).fetchall() == EXPECTED
        calendar = next(row for row in runtime.config.entities if row.calendar_id == "fiscal")
        assert calendar.key == ["date_id"]
        assert not any(
            row.entity == calendar.id and row.column == "date_day"
            for row in runtime.config.dimensions
        )
        assert runtime.adapter._db.conn.execute(
            "SELECT COUNT(*) = COUNT(DISTINCT date_id), MIN(date_day)::TIME "
            "FROM jaffle_calendar_fiscal"
        ).fetchone() == (True, datetime(2024, 5, 1, 12).time())
        with patch.object(runtime.adapter, "query", wraps=runtime.adapter.query) as execute:
            if path == "leaf_binding":
                config = load_package_config(
                    resolve_repo_path("configs/semantic_rails/jaffle_shop")
                )
                plan = compile_query(config, Registry(config), _query("fiscal"))["logical_plan"]
                assert _calendar_fill_binding(plan, runtime.config) == (
                    "jaffle_calendar_fiscal",
                    "week_start",
                    None,
                )
            with pytest.raises(SemanticLayerError) as refused:
                if path == "runtime":
                    runtime.query(_query("fiscal"))
                else:
                    _leaf_calendar_binding(plan, runtime.config)
            execute.assert_not_called()
        assert refused.value.code == "REWRITE_NOT_SUPPORTED"
        assert refused.value.details == {
            "calendar_id": "fiscal",
            "column": "date_day",
            "declared_type": None,
        }
        assert "declare `date_day` as a date" in str(refused.value)
    finally:
        runtime.close()


@pytest.mark.parametrize("binding", [_calendar_fill_binding, _leaf_calendar_binding])
def test_calendar_binding_refuses_a_forced_timestamp_anchor(binding):
    runtime = _runtime("fiscal")
    try:
        assert runtime.adapter._db.conn.execute(REFERENCE).fetchall() == EXPECTED
        config = runtime.config
        plan = compile_query(config, Registry(config), _query("fiscal"))["logical_plan"]
        changed = replace(
            config,
            dimensions=[
                replace(row, data_type="timestamp")
                if row.entity == "entity.jaffle_fiscal_calendar" and row.column == "week_start"
                else row
                for row in config.dimensions
            ],
        )
        with pytest.raises(SemanticLayerError) as refused:
            binding(plan, changed)
        assert refused.value.code == "REWRITE_NOT_SUPPORTED"
    finally:
        runtime.close()
