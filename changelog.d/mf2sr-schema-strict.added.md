- `mf2sr --schema-strict`, and `semantic-rails import --from metricflow --schema-strict`, write a
  `schema_strict: true` package whose relations keep the schema, and on Snowflake, BigQuery and
  Databricks the database, that dbt's `semantic_manifest.json` records. The output is
  parse-checked, and each strict error is a warning. See [mf2sr/README.md](mf2sr/README.md).
