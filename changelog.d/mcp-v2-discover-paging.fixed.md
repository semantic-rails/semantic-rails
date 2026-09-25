- Query MCP interface v2: `discover` with empty `terms` lists at most 100 ids
  per kind at a time, so its response stays bounded on large packages. `limit`
  and `offset` page the ids, and a `DISCOVER_IDS_TRUNCATED` warning says which
  kinds have more. An unknown tool name on v2 now gets a hint that names only
  v2 tools, not `validate` and `compile`.
