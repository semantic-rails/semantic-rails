"""The empty-result coverage probe never builds SQL from package text."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from semantic_rails.db import WarehouseAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import _coverage_probe_query, _data_coverage_probe
from semantic_rails.schema import DimensionConfig, EntityConfig, TemporalRoleConfig


class _RecordingAdapter(WarehouseAdapter):
    engine = "duckdb"

    def __init__(self) -> None:
        self.sql: list[str] = []

    def query(self, sql: str, *, limits: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.sql.append(sql)
        return [{"min_t": "2016-09-01", "max_t": "2017-08-31"}]

    def close(self) -> None:
        pass


def _config(table: str, column: str) -> SimpleNamespace:
    return SimpleNamespace(
        entities=[EntityConfig(id="entity.order", table=table, primary_key="order_id")],
        dimensions=[
            DimensionConfig(
                id="dimension.ordered_at", entity="entity.order", column=column, data_type="date"
            )
        ],
        temporal_roles=[
            TemporalRoleConfig(
                id="temporal_role.order_time",
                dimension="dimension.ordered_at",
                temporal_class="event",
            )
        ],
    )


def _probe(adapter: WarehouseAdapter, table: str, column: str) -> dict[str, str]:
    return _data_coverage_probe(
        adapter,
        _config(table, column),
        warehouse="duckdb",
        root_entity="entity.order",
        temporal_role="temporal_role.order_time",
        limits={},
    )


HOSTILE_TABLES = [
    'orders"; DROP TABLE orders; --',
    "orders; SELECT 1",
    "orders UNION ALL SELECT 1, 2",
    "read_csv('/etc/passwd')",
    "read_csv_auto('data.csv')",
    "/etc/passwd",
    "../other/warehouse",
    '"orders"',
    "orders -- comment",
    "a.b.c.d",
    "analytics..orders",
    " orders",
    "1orders",
]
HOSTILE_COLUMNS = [
    "ordered_at) AS min_t, (SELECT content FROM read_text('/etc/passwd')) AS max_t FROM orders --",
    'ordered_at"',
    "ordered_at; DROP TABLE orders",
    "'ordered_at'",
    "ordered at",
    "orders.ordered_at",
    "ordered_at\n",
]


@pytest.mark.parametrize(
    ("table", "column"),
    [*((table, "ordered_at") for table in HOSTILE_TABLES)]
    + [("orders", column) for column in HOSTILE_COLUMNS],
)
def test_names_that_are_not_plain_identifiers_never_reach_the_warehouse(
    table: str, column: str
) -> None:
    adapter = _RecordingAdapter()

    assert _probe(adapter, table, column) == {}
    assert adapter.sql == []

    with pytest.raises(SemanticLayerError) as exc:
        _coverage_probe_query(
            "duckdb", table, column, entity="entity.order", dimension="dimension.ordered_at"
        )
    assert exc.value.code == "INVALID_CONFIG"
    assert exc.value.details == {
        "reason": "probe_identifier_not_plain",
        "entity": "entity.order",
        "dimension": "dimension.ordered_at",
    }


@pytest.mark.parametrize(
    ("warehouse", "table", "column", "sql"),
    [
        (
            "duckdb",
            "analytics.orders",
            "ordered_at",
            "SELECT\n  MIN(ordered_at) AS min_t,\n  MAX(ordered_at) AS max_t\nFROM analytics.orders",
        ),
        # A reserved word is quoted, as the compiled query quotes it.
        ("duckdb", "orders", "order", 'MIN("order") AS min_t'),
        ("bigquery", "shop.orders", "order", "MIN(`order`) AS min_t"),
        ("snowflake", "DB.SHOP.ORDERS", "ORDERED_AT", "FROM DB.SHOP.ORDERS"),
    ],
)
def test_plain_names_render_as_the_compiler_renders_them(
    warehouse: str, table: str, column: str, sql: str
) -> None:
    query = _coverage_probe_query(
        warehouse, table, column, entity="entity.order", dimension="dimension.ordered_at"
    )
    assert sql in query.sql


def test_plain_names_report_the_data_coverage() -> None:
    adapter = _RecordingAdapter()

    assert _probe(adapter, "orders", "ordered_at") == {"min": "2016-09-01", "max": "2017-08-31"}
    assert adapter.sql == [
        "SELECT\n  MIN(ordered_at) AS min_t,\n  MAX(ordered_at) AS max_t\nFROM orders"
    ]
