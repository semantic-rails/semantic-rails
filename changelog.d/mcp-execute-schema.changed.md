- MCP `execute`'s tool definition is less than half its size (12,831 → 5,991 characters), so
  each session's `tools/list` costs less. Its `query` schema has the same fields, words and rules
  as `schemas/query.v2.json`, with each expression kind's keys as one line of a table and the
  filter words' value shapes and SQL spellings in one sentence; `query.v2.json` keeps a schema
  per kind. `query.v2.json` now refuses an entity filter that pairs `any` or `none` with
  `having`, as the parser does.
