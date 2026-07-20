#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DB_PATH="$PACK_DIR/../shared/data/jaffle_comparison.duckdb"
OUTPUT_DIR="${1:-$PACK_DIR/trial_data}"
DUCKDB_BIN="${DUCKDB_BIN:-$(command -v duckdb)}"

if [[ -z "${DUCKDB_BIN:-}" ]]; then
  echo "duckdb binary not found on PATH" >&2
  exit 1
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "DuckDB database not found at $DB_PATH" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

tables=(
  comparison_orders
  comparison_order_items
  comparison_customers
  comparison_stores
  comparison_customer_history
  comparison_order_lifecycle
  comparison_storefront_sessions
)

for table in "${tables[@]}"; do
  output_file="$OUTPUT_DIR/${table}.csv"
  "$DUCKDB_BIN" "$DB_PATH" -c "COPY ${table} TO '${output_file}' (HEADER, DELIMITER ',');" >/dev/null
  row_count="$("$DUCKDB_BIN" -csv -noheader "$DB_PATH" "SELECT COUNT(*) FROM ${table};" | tr -d '[:space:]')"
  printf '%-32s %8s rows -> %s\n' "$table" "$row_count" "$output_file"
done
