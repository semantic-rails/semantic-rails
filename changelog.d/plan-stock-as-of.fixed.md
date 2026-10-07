- `plan` no longer returns `ok` for a balance read over more than one day per row. A stock
  measure keyed by a series column besides its clock answers with each series' last snapshot,
  so with no time block, or by week or month, a closed account still added its last value. Such
  a draft is now held with the `stock_as_of_unrealized` coverage gap. Stock reads through
  predicates remain held at every grain, since the
  predicate can have its own time scope. For a directly selected balance, ask for one day
  ("MRR yesterday") or set `time.grain: day`; recovery examples use a date placeholder.
  See [docs/MCP_INTERFACE.md](docs/MCP_INTERFACE.md#plan).
