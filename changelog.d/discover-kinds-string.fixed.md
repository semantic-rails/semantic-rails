- `discover` no longer returns empty results for a `kinds` filter that arrived as a
  JSON-encoded string such as `"[\"metric\"]"`: it reads the same as the array. A `kinds`
  value that does not parse, or names a kind the search cannot rank, is refused with
  `INVALID_MCP_ARGUMENTS` (HTTP `400`) instead of returning an empty result, and the
  `DISCOVER_UNKNOWN_KIND` warning is gone. A misspelled `kind` argument still warns, and the
  recovery hint no longer claims nothing matched.
