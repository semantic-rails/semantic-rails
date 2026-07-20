# Snowflake Semantic Views Comparison Pack

This folder is an executed Snowflake Semantic Views pack backed by the default Snowflake CLI connection `semantic_views_trial`.

## Included Artifacts

- `jaffle_semantic_view.yaml`: a compact semantic-view YAML for the shared Jaffle subset.
- `query_examples.sql`: the 16-question suite used by the Snowflake runner.
- `trial_setup.sql`: warehouse/database/schema bootstrap plus explicit table DDL for a fresh trial account.
- `load_trial_csvs.sql`: `COPY INTO` statements for the staged trial CSVs.
- `verify_trial_load.sql`: row-count checks for the seven `COMPARISON_*` tables after upload.
- `scripts/export_trial_csvs.sh`: exports the local comparison tables from DuckDB to CSV for Snowsight upload.
- `scripts/upload_trial_csvs.sh`: uploads all exported CSVs to a Snowflake stage and executes the load SQL.
- `scripts/render_create_semantic_view_sql.py`: generates `generated/create_semantic_view.sql` from the YAML.
- `scripts/run_questions.py`: verifies the base tables, recreates the semantic view, captures `SHOW` / `DESCRIBE` output, runs the 16-question suite, and writes `shared/results/snowflake_semantic_views/`.

## Fairness Notes

- The pack models the same 4-table core plus the 3 stretch tables used elsewhere in the comparison.
- `q01`-`q07` execute natively through `SEMANTIC_VIEW(...)`.
- Historical segmenting, predicate-heavy edge cases, and session-to-order conversion variants are represented as executed verified SQL workarounds rather than claimed as semantic-view-native behavior.
- Relationship type inference is left to Snowflake, consistent with the Semantic Views YAML spec.

## Executed Outcome

- Semantic view: `ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON`
- Connection: `semantic_views_trial`
- Support: `7 native`, `9 workaround`
- Output consistency: all 16 questions match the normalized `Semantic Rails` reference outputs.

## Trial Account Workflow

1. Run `trial_setup.sql`.
2. Run `scripts/export_trial_csvs.sh`.
3. Run `scripts/upload_trial_csvs.sh`.
4. Run `scripts/run_questions.py`.
