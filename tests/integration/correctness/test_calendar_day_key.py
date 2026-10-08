"""Calendar day joins require declarations proving one calendar row per day."""

from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.config import _load_package_source, _parse_package
from semantic_rails.config_parts.package_loader import normalize_package
from semantic_rails.db import Database, DuckDBAdapter
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry

from .conftest import SEED, SHOP
from .test_correctness import ORDERS, ROLE, _backend

REFERENCE = """
    SELECT bucket, COUNT(DISTINCT o.order_id) AS orders
    FROM (VALUES (DATE '2024-05-06'), (DATE '2024-05-13')) AS weeks(bucket)
    LEFT JOIN orders AS o
      ON o.ordered_at >= bucket AND o.ordered_at < bucket + INTERVAL '7 days'
    GROUP BY bucket ORDER BY bucket
"""


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("declaration", ["surrogate_key", "day_key", "undeclared_day"])
def test_calendar_day_key_controls_repeated_day_join(request, backend_name, declaration):
    declared_day_key = declaration == "day_key"
    backend = _backend(request, backend_name)
    source = backend.runtimes["utc_authored"]
    adapter = source._get_adapter()
    reference_rows = backend.reference
    if backend_name == "duckdb":
        # The shared backend seed is read-only; mutations use a private in-memory seed.
        adapter = DuckDBAdapter.__new__(DuckDBAdapter)
        adapter._db = Database.connect_in_memory()
        adapter._db.conn.execute(SEED)

        def reference_rows(sql):
            return adapter._db.conn.execute(sql).fetchall()

    table = f"calendar_days_{uuid4().hex}"
    copies = "(VALUES (1))" if declared_day_key else "(VALUES (1), (2))"
    reference_rows(
        f"CREATE TABLE {table} AS SELECT c.*, "
        "ROW_NUMBER() OVER (ORDER BY c.date_day, copies.n) AS date_id "
        f"FROM dim_fiscal c CROSS JOIN {copies} AS copies(n)"
    )
    try:
        assert reference_rows(
            f"SELECT COUNT(*) = COUNT(DISTINCT date_id), "
            f"COUNT(*) = COUNT(DISTINCT date_day) FROM {table}"
        ) == [(True, declared_day_key)]
        reference = reference_rows(REFERENCE)
        assert reference == [(date(2024, 5, 6), 1), (date(2024, 5, 13), 0)]
        # The unguarded day join doubles the first week's count for the surrogate key.
        assert reference_rows(
            f"SELECT COUNT(*) FROM orders o JOIN {table} c "
            "ON c.date_day = CAST(o.ordered_at AS DATE) "
            "WHERE o.ordered_at >= DATE '2024-05-06' AND o.ordered_at < DATE '2024-05-13'"
        ) == [(1 if declared_day_key else 2,)]
        key = ["date_day"] if declared_day_key else ["date_id"]
        raw = _load_package_source(str(SHOP))
        raw["graph"]["entities"]["fiscal_calendar"]["key"] = key
        if declaration == "undeclared_day":
            del raw["models"]["fiscal_calendar"]["times"]["date_day"]
        authored = _parse_package(normalize_package(raw), path=str(SHOP))
        config = replace(
            authored,
            package=source.config.package,
            aggregate_relations=[],
            entities=[
                replace(row, table=table) if row.calendar_id == "fiscal" else row
                for row in authored.entities
            ],
        )
        query = {
            "version": 1,
            "select": [{"expression": ORDERS, "as": "orders"}],
            "time": {
                "temporal_role": ROLE,
                "grain": "week",
                "start": "2024-05-06",
                "end": "2024-05-20",
                "fill": True,
                "calendar_id": "fiscal",
            },
            "order_by": [{"field": f"{ROLE}__week"}],
        }
        if declared_day_key:
            compiled = compile_query(config, Registry(config), query)
            rows = adapter.query(compiled["sql"])
            assert [
                (date.fromisoformat(str(row[f"{ROLE}__week"])[:10]), row["orders"]) for row in rows
            ] == reference
        else:
            with pytest.raises(SemanticLayerError) as refused:
                compile_query(config, Registry(config), query)
            assert refused.value.code == "REWRITE_NOT_SUPPORTED"
            assert refused.value.details == {
                "reason": "calendar_day_key_unproven",
                "calendar_id": "fiscal",
                "column": "date_day",
                **(
                    {"declared_type": None}
                    if declaration == "undeclared_day"
                    else {"declared_key": ["date_id"]}
                ),
            }
    finally:
        reference_rows(f"DROP TABLE {table}")
        if backend_name == "duckdb":
            adapter.close()
