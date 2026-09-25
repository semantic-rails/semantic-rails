- Read-only warehouse introspection for authoring, in
  `semantic_rails.architect_introspection` and as Architect MCP tools:
  `list_tables`, `describe_table` (types, nullability, declared keys),
  `profile_columns` (counts, min/max, capped samples) and `suggest_model`
  (key, time, dimension, measure and foreign-key candidates, each with a
  confidence and a reason, plus draft `upsert_model` arguments). They read
  DuckDB databases, including one dbt builds, and never write to them.
