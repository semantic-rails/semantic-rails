- A filtered count or sum asked with no time window, over a table that declares how far it is
  loaded (`zero_through`), reads `0` when its filter has never matched a loaded row, such as
  "how many enterprise accounts have we ever had?" before the first one. It read `NULL` with
  `NO_DATA_IN_SCOPE`, although the same table's loaded weeks already read `0`. An empty load
  log still reads `NULL`, and so does a model whose edge is `data`.
