- `discover` no longer returns empty results for a `kinds` filter that arrived as a
  JSON-encoded string such as `"[\"metric\"]"`: it reads the same as the array. A `kinds`
  value that does not parse is refused (`INVALID_MCP_ARGUMENTS` over MCP, `INVALID_REQUEST`
  over HTTP, both `400`-class), and one that names a kind the search cannot rank is refused
  with `INVALID_MCP_ARGUMENTS` (HTTP `400`) and a `use_valid_kind` recovery hint that names
  the valid kinds, instead of returning an empty result; the `DISCOVER_UNKNOWN_KIND` warning
  is gone. The CLI `--kinds` flag reads the same encodings. An MCP or HTTP `limit` below 1 is
  refused rather than emptying every bucket. A misspelled `kind` argument still warns, and the recovery
  hint no longer claims nothing matched.
- The `discover` no-match hint now follows the response's own `no_matches` signal: it no longer
  appears beside matching dimension values, and a search that was screened out before it ran
  (`low_relevance`, `out_of_scope`) does not claim a kind-scoped search found nothing. In
  resource-grant mode the same refusal applies to any kind a grant cannot produce (only
  `metric`, `dimension` and `temporal_role` are searched, on MCP and HTTP alike), and it keeps
  its `valid_kinds` detail; the empty-terms listing omits the kinds a grant cannot produce,
  and a grant search never reports `no_matches`, because it covers a filtered view.
