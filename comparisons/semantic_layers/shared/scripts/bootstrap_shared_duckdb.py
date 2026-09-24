from __future__ import annotations

import hashlib
from pathlib import Path

from semantic_rails.db import Database, load_csv_dir_to_duckdb

REPO_ROOT = Path(__file__).resolve().parents[4]
DB_PATH = (
    REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "data" / "jaffle_comparison.duckdb"
)
CSV_DIR = REPO_ROOT / "data" / "jaffle_csv"
POST_SQL = REPO_ROOT / "data" / "seed_jaffle.sql"

# Every layer reads only these views, so they answer each question from the same data.
COMPARISON_VIEWS_SQL = """
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
SELECT * FROM jaffle_order_lifecycle;

CREATE OR REPLACE VIEW comparison_storefront_sessions AS
SELECT * FROM jaffle_storefront_session;
""".strip()


def dataset_fingerprint() -> str:
    """Identify the shared dataset: the seed files plus the comparison views over them.

    Runners record this with their results, so a capture made on older data is detectable.
    """
    digest = hashlib.sha256()
    parts = [
        (path.relative_to(REPO_ROOT).as_posix(), path.read_bytes())
        for path in sorted(CSV_DIR.glob("*.csv"))
    ]
    parts += [(POST_SQL.relative_to(REPO_ROOT).as_posix(), POST_SQL.read_bytes())]
    parts += [("comparison views", COMPARISON_VIEWS_SQL.encode("utf-8"))]
    for name, data in parts:
        data = data.replace(b"\r\n", b"\n")  # the same data on a CRLF checkout
        digest.update(f"{name}\0{len(data)}\0".encode())
        digest.update(data)
    return digest.hexdigest()


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    load_csv_dir_to_duckdb(str(DB_PATH), str(CSV_DIR), str(POST_SQL))
    fingerprint = dataset_fingerprint()
    db = Database.connect(str(DB_PATH))
    try:
        db.execute_script(COMPARISON_VIEWS_SQL)
        db.execute_script(
            f"CREATE OR REPLACE TABLE comparison_dataset AS SELECT '{fingerprint}' AS fingerprint;"
        )
    finally:
        db.close()
    print(f"Built shared comparison DuckDB at {DB_PATH} (dataset {fingerprint[:12]})")


if __name__ == "__main__":
    main()
