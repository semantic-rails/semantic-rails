- A `where` item can now say which child rows its conditions mean: a child group
  `{child, match, where}` keeps a row when at least one of its child rows meets every
  condition (`match: any`), or when none does (`match: none`). "Customers with an item that
  is a beverage and costs over 5" is one group; "a beverage, and some item over 5" is two.
  Groups compile to correlated `EXISTS` / `NOT EXISTS`, so no parent is counted twice. A
  query states one child scope: beside a group nothing else may cross a one-to-many hop,
  and groups on different children are refused with `MIXED_GRAIN_INVALID`.
- Two kinds of plain filters on one child entity are now refused with
  `AMBIGUOUS_CHILD_SCOPE` instead of `MIXED_GRAIN_INVALID`: two or more positive filters
  (`same_row` / `separate_rows`), and one negated filter (`!=`, `NOT IN`, `IS NULL`, ...:
  `any_not` / `none`). Its `details.clarification.options` holds each reading as the
  query's whole rewritten `where`, ready to resend. It is offered only when every reading
  answers for this caller: each option is bound (every measure, the warehouse's rules, row
  policies) and passes the caller's semantic policies. Otherwise, and for every other
  shape (a negated filter beside another, filters on different children, an operator such
  as `IS DISTINCT FROM` or `NOT ILIKE`), the query keeps `MIXED_GRAIN_INVALID`. So does one
  whose group on the child would take another route than the filters' own (the refusal
  names the `graph.path_preferences` row to record).
- An ambiguous route to a group's child is refused with `AMBIGUOUS_PATH`. A group is
  refused under a row policy, never reads a rollup, and on ClickHouse only one `any` group
  on the child's own columns is answered.
- Explicit child groups are unavailable under restricted metric and dimension grants,
  which do not provide authority for caller-selected child entity scopes.
