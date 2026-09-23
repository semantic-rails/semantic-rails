"""The DuckDB runtime never replaces a warehouse file its package did not create."""

from __future__ import annotations

from pathlib import Path

from semantic_rails.runtime import Runtime
from tests.semantic_rails.dbt_warehouse import (
    ORDER_COUNT_QUERY,
    build_dbt_warehouse,
    file_digest,
    write_orders_package,
)


def test_dbt_built_database_in_custom_schemas_is_not_replaced(tmp_path: Path) -> None:
    """Regression: the existence check looked only in schema ``main``, so a
    package reading ``main_marts.*`` rebuilt its placeholder seed over the
    dbt-built file and the dbt output was lost."""
    package_dir = write_orders_package(tmp_path)
    db_path = build_dbt_warehouse(package_dir / "data" / "warehouse.duckdb")
    before = file_digest(db_path)

    runtime = Runtime.from_path(str(package_dir))
    try:
        try:
            rows = runtime.query(ORDER_COUNT_QUERY)["rows"]
        finally:
            assert file_digest(db_path) == before, "the runtime replaced the dbt-built database"
    finally:
        runtime.close()
    assert rows == [{"orders": 8}]
