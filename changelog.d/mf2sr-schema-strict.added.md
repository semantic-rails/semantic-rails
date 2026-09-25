- `mf2sr --schema-strict`, and `semantic-rails import --from metricflow --schema-strict`, write a
  `schema_strict: true` package whose relations keep the schema and database that dbt's
  `semantic_manifest.json` records, named the way `import_dbt_project` names dbt relations. A DuckDB
  package reads the database dbt built (`seed: {kind: external}`), and the output is parse-checked.
  See [mf2sr/README.md](mf2sr/README.md).
