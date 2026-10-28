- `plan` drafts the total a question asks for: "with the grand total" as `totals: []`,
  "with subtotals by store" as `totals` keeping the store, "with each store's total across
  months" as a `total` item, and "each store's share of revenue" as a `share` item, validated
  as `execute` reads them. When the engine refuses the total (a policy, withheld values, a
  share of an average) or the draft has no place for it, the draft comes back without it,
  `low_confidence`, with `PLAN_TOTALS_NOT_DRAFTED` naming why. See
  [Plan](docs/MCP_INTERFACE.md#plan).
