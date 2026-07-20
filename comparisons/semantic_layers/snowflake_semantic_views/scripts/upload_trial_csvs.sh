#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="${1:-$PACK_DIR/trial_data}"
CONNECTION_NAME="${SNOWFLAKE_CONNECTION_NAME:-semantic_views_trial}"
STAGE_PATH='@ANALYTICS.SEMANTIC_COMPARISON.SEMANTIC_LOAD_STAGE'

if [[ ! -d "$DATA_DIR" ]]; then
  echo "CSV directory not found at $DATA_DIR" >&2
  exit 1
fi

snow sql -c "$CONNECTION_NAME" -q \
  "USE ROLE ACCOUNTADMIN; USE WAREHOUSE SEMANTIC_WH; USE DATABASE ANALYTICS; USE SCHEMA SEMANTIC_COMPARISON; CREATE STAGE IF NOT EXISTS SEMANTIC_LOAD_STAGE;"

snow stage copy -c "$CONNECTION_NAME" "$DATA_DIR/*.csv" "$STAGE_PATH" --overwrite --parallel 8

snow sql -c "$CONNECTION_NAME" -f "$PACK_DIR/load_trial_csvs.sql"
