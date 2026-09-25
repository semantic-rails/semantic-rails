- `semantic_rails.dbt_artifacts` reads a dbt project's `manifest.json` and
  `catalog.json` (dbt never runs) and suggests a model per dbt model, keeping
  its schema-qualified relation: keys from contracts, `unique` + `not_null`
  and `unique_combination_of_columns` tests, foreign keys from `relationships`
  tests, value sets from `accepted_values`, and descriptions. The Architect
  MCP exposes it as `suggest_models_from_dbt`.
