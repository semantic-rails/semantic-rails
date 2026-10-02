- A `where` item can now say which child rows its conditions mean: a child group
  `{child, match, where}` keeps a row when at least one of its child rows meets every
  condition (`match: any`), or when none does (`match: none`). "Customers with an item that
  is a beverage and costs over 5" is one group; "a beverage, and some item over 5" is two.
  Groups compile to correlated `EXISTS` / `NOT EXISTS`, so no parent is counted twice.
- Two or more plain filters on one child entity, a plain filter beside an `any` group on its
  child, and a negated plain filter on a child (`!=`, `NOT IN`, `IS NULL`, ...) are now
  refused with `AMBIGUOUS_CHILD_SCOPE` instead of `MIXED_GRAIN_INVALID`. Its
  `details.clarification.options` holds each reading (`same_row` / `separate_rows`, or
  `any_not` / `none`) as the query's whole rewritten `where`, ready to resend. Beside
  several groups there is a `same_row_where_<i>` option per group, and a negated filter
  beside other conditions keeps its `none` reading (`none_where_<i>` when there are
  several). Every option is bound before it is offered (every measure, the warehouse's
  rules, row policies); a query with no answerable reading, or too many to check, retains
  `MIXED_GRAIN_INVALID`, and so does one whose group on the child would take another route
  than the filters' own (the refusal names the `graph.path_preferences` row to record). A
  positive plain filter beside a `none` group needs no clarification.
- An ambiguous route to a group's child is refused with `AMBIGUOUS_PATH`. A group is
  refused under a row policy, never reads a rollup, and on ClickHouse only one `any` group
  on the child's own columns is answered.
- Explicit child groups are unavailable under restricted metric and dimension grants,
  which do not provide authority for caller-selected child entity scopes.
