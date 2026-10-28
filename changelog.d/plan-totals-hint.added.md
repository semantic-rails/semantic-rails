- `plan` hints the call shape for a total it doesn't draft. When a question asks for a grand
  total, subtotals, each group's total or each group's share, and its plan isn't `ok`, its
  `warnings` carry `PLAN_TOTALS_HINT` (severity `info`) with `details.add`: the `totals` or the
  `total`/`share` select item to add, named with `best.query`'s ids. The plan's status and
  `best.query` don't change. See [Plan](docs/MCP_INTERFACE.md#plan).
