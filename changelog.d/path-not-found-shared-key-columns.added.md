- A call refused with `PATH_NOT_FOUND` because no relationship chain joins its start to its
  target now names the key columns the two share (`details.shared_key_columns`) and whether
  they hold the target's whole key (`shared_key_unique_on_target`). The message says when they
  share no key column, or when one start row can match many target rows on the columns they
  share, and the split hint says so too: a total across rows matched on a key that isn't
  unique on the far side counts a start row once per match. A load or snapshot date on both
  tables is not a shared key, unless it is one side's whole key (a calendar's date). The
  author hint gives the relationship row on the shared columns with both cardinalities it can
  declare: many-to-one when no two far-side rows hold the same values, otherwise many-to-many,
  which a call can't group or filter through; with no shared key, it says where a
  relationship goes in a `graph.yml` package. See the `PATH_NOT_FOUND` row in
  [docs/MCP_INTERFACE.md](docs/MCP_INTERFACE.md).
