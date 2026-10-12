- MCP `execute`'s tool definition is less than half its size (12,831 → 6,154 characters), so
  each session's `tools/list` costs less. Its `query` schema has the same fields, words and rules
  as `schemas/query.v2.json`, with each expression kind's keys as one line of a table; its op
  enum lists the filter words and the SQL aliases read as them (`=`, `!=`, `<>`, `>`, `IN`,
  `NOT IN`, ..., in lower case and capitals), so a client that checks a call against the schema
  accepts them, and one sentence states each word's values. `query.v2.json` keeps a schema per
  kind. `query.v2.json` now refuses an entity filter that pairs `any` or `none` with `having`,
  as the parser does.
