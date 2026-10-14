- `discover` reads a plural kind (`measures`, `entities`) as its singular, and the MCP tool's
  `kinds` schema lists the kinds it ranks.
- A `valid-values` call that reads nothing because the dimension declares no value domain now
  names the dimension and the argument that returns its values (`allow_live_query: true`),
  which a first call may pass too.
