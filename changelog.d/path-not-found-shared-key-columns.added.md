- A call refused with `PATH_NOT_FOUND` because no relationship chain joins its start to its
  target now names the key columns the two share (`details.shared_key_columns`) and whether
  they hold the target's whole key (`shared_key_unique_on_target`). The message says when they
  share no key column, or when one start row can match many target rows on the columns they
  share, and the split hint says so too: a total across rows matched on a key that isn't
  unique on the far side counts a start row once per match. The author hint says that no
  many-to-one relationship joins such a pair on those columns, and otherwise where a
  relationship goes in a `graph.yml` package. See the `PATH_NOT_FOUND` row in
  [docs/MCP_INTERFACE.md](docs/MCP_INTERFACE.md).
