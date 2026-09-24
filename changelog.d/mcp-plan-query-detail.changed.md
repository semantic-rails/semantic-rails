- MCP `plan` supports opt-in `detail="query"`: `status`, `best.query_ir`, and any `why` or
  `warnings`. An unchanged v1 call keeps the `best` response, including `intent_ir`,
  `best.trace` and `next`. The HTTP API also keeps `detail="best"`.
