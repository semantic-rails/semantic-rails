- The Architect MCP's `import_dbt_project` creates or updates package models
  from selected dbt models in one transaction (with dry run, revision and
  idempotency checks), writing each `relationships` test as an entity
  reference so the engine can join across them. `ArchitectProject.upsert_models`
  stages several models and their foreign-key references in one transaction.
