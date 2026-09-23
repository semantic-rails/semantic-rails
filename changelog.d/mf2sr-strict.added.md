- `mf2sr --schema-strict` writes a `schema_strict: true` package: relations
  keep their schema, ratio and derived metrics get an explicit `value_type`,
  and the output is parse-checked. `--dbt-target` resolves `ref()` and
  `source()` relations through dbt's `manifest.json`, and makes a DuckDB
  package read the dbt-built database (`seed: {kind: external}`).
