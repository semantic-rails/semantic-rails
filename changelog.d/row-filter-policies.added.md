- A new `row_filter` package policy limits a relation's rows to a trusted request attribute,
  for example each customer's own orders: the compiler adds `<column> = ?` and the runtime
  binds the host's `TrustedAttributes` value (DuckDB only today). A missing or mistyped
  attribute is denied on every surface, as is any query that reads more than the one filtered
  relation (joins, metric filters, calendar spines); rollups aren't routed to under a row
  filter, and the zero-row coverage probe is skipped. The package loader rejects a row filter
  it can't enforce, and a policy that looks like a misspelled one. A warehouse error on a
  parameterized statement is now raised without the driver's message. See
  [docs/PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md).
