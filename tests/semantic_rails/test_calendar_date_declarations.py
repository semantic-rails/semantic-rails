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
from semantic_rails.config import load_package_config, resolve_repo_path
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


def _runtime(calendar_id="default", column="week_start", declared_type="date"):
    source = resolve_repo_path("configs/semantic_rails/jaffle_shop")
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
