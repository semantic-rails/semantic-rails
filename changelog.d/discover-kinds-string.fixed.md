- `discover` no longer returns empty results for a `kinds` filter that arrived as a
  JSON-encoded string such as `"[\"metric\"]"`: it reads the same as the array. A `kinds`
  value that does not parse, or names a kind the search cannot rank, is refused with
  `INVALID_MCP_ARGUMENTS` (HTTP `400`) instead of returning an empty result, and the
  `DISCOVER_UNKNOWN_KIND` warning is gone. A misspelled `kind` argument still warns, and the
  recovery hint no longer claims nothing matched.
- The `discover` no-match hint now follows the response's own `no_matches` signal: it no longer
  appears beside matching dimension values, and a search that was screened out before it ran
  (`low_relevance`, `out_of_scope`) does not claim a kind-scoped search found nothing. An unknown
  kind is refused with a `use_valid_kind` recovery hint that names the valid kinds, and the same
  refusal applies in resource-grant mode. The CLI `--kinds` flag and `roles` in a policy context
  read a JSON array in a string, and a value that does not parse is refused.
