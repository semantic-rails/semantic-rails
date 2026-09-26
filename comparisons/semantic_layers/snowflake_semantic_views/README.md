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
- Range joins, announced in preview on 2026-02-25, could express the temporal-validity joins in q08 and q16 inside the semantic view. This capture does not model them yet.
- Relationship type inference is left to Snowflake, consistent with the Semantic Views YAML spec.

## Executed Outcome

- Captured: `2026-04-06T23:05:57-04:00` (2026-04-07 UTC), on a trial account, on an earlier dataset. It has not been re-run since, so the output check reports it separately from the layers checked on the current dataset. The captured `create_semantic_view.json` and `describe_semantic_view.json` reflect `jaffle_semantic_view.yaml` as it stood then.
- Semantic view: `ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON`
- Connection: `semantic_views_trial`
- Support labels, from the rubric (`../shared/rubric.md`): `7 native`, `7 workaround`, `2 precomputed` (q11, q12), `8 not_assessed` (q17-q24): the frozen-model questions need a live account to run
- Output check: this capture matches the independent answer key (`../shared/oracle/`) on 14 of 16 questions. q07 and q16 do not: this capture loaded the `comparison_order_lifecycle` view when it held only the 11 hand-authored lifecycle rows. The view now passes every order through. Re-running `scripts/export_trial_csvs.sh` and the steps below would load the current data.

## Dataset Provenance

`scripts/export_trial_csvs.sh` also exports `comparison_dataset`, the shared dataset's
fingerprint, and `load_trial_csvs.sql` loads it into `COMPARISON_DATASET`. The runner reads it
back and records it as `dataset_fingerprint` in its summary. A run is compared with the other
layers only when that fingerprint matches the current dataset. Otherwise, as for the
2026-04-06 capture that predates this table, it is reported as stale.

## Trial Account Workflow

1. Run `trial_setup.sql`.
2. Run `scripts/export_trial_csvs.sh`.
3. Run `scripts/upload_trial_csvs.sh`.
4. Run `scripts/run_questions.py`.
