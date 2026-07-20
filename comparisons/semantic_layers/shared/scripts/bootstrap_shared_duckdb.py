from __future__ import annotations

from pathlib import Path

from semantic_rails.db import Database, load_csv_dir_to_duckdb

REPO_ROOT = Path(__file__).resolve().parents[4]
DB_PATH = (
    REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "data" / "jaffle_comparison.duckdb"
)
CSV_DIR = REPO_ROOT / "data" / "jaffle_csv"
POST_SQL = REPO_ROOT / "data" / "seed_jaffle.sql"


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    load_csv_dir_to_duckdb(str(DB_PATH), str(CSV_DIR), str(POST_SQL))
    db = Database.connect(str(DB_PATH))
    try:
        db.execute_script(
            """
            CREATE OR REPLACE VIEW comparison_orders AS
            SELECT * FROM jaffle_order;

            CREATE OR REPLACE VIEW comparison_order_items AS
            SELECT
              i.*,
              o.ordered_at,
              o.store_id,
              o.customer_id
            FROM jaffle_item AS i
            INNER JOIN jaffle_order AS o
              ON i.order_id = o.order_id;

            CREATE OR REPLACE VIEW comparison_customers AS
            SELECT * FROM jaffle_customer;

            CREATE OR REPLACE VIEW comparison_stores AS
            SELECT * FROM jaffle_store;

            CREATE OR REPLACE VIEW comparison_customer_history AS
            SELECT * FROM jaffle_customer_history;

            CREATE OR REPLACE VIEW comparison_order_lifecycle AS
            SELECT lifecycle.*
            FROM jaffle_order_lifecycle AS lifecycle
            INNER JOIN raw_order_lifecycle_events AS raw
              ON lifecycle.order_id = CAST(raw.order_id AS VARCHAR);

            CREATE OR REPLACE VIEW comparison_storefront_sessions AS
            SELECT * FROM jaffle_storefront_session;
            """.strip()
        )
    finally:
        db.close()
    print(f"Built shared comparison DuckDB at {DB_PATH}")


if __name__ == "__main__":
    main()
