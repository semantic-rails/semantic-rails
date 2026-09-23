- MCP `plan` defaults to `detail="query"`: `status`, `best.query_ir`, and any `why` or
  `warnings`. Pass `detail="best"` for `intent_ir`, `best.trace` and the `next` block. The
  HTTP API keeps `detail="best"`.
