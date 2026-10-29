- `discover` takes `terms` as a list (`["revenue", "region"]`) over MCP and REST: each term is
  ranked on its own, exactly as a call with that term alone, and the results come back keyed by
  term under `by_term`, with the call's own fields once beside them. The agent splits its
  question into terms; a list's terms are never ranked together, and a string is one search as
  before. When the terms' results don't fit the response budget, the leading terms that fit are
  returned and a `DISCOVER_TERMS_TRUNCATED` warning names the rest to send in another call. See [Catalog And Metadata](docs/MCP_INTERFACE.md#catalog-and-metadata).
