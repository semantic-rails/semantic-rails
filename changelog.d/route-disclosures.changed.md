- An answer whose only route goes through another table's rows and up to another parent
  ("the Team of any of the Member's Member events") now carries a `ROUTE_PASS_THROUGH`
  warning at every verbosity: a member with no event is left out and one with several can
  count under several teams. It names the fixes: declare the key, declare the table a link
  table with `bridge: true` (now kept on the entity), record the route, or ask with a child
  group. Answers and routes are unchanged.
  The next release refuses a to-one lookup ("the T of an S") reachable only through a
  child table's rows: declare the key in `entities:` to make that lookup explicit. A link
  table declared with `bridge: true` keeps answering existence and group questions through
  it. Upgrade now: for each `ROUTE_PASS_THROUGH` warning, declare the key or mark the link
  table. The warning also appears on `plan` drafts and in the route census and project
  status, with the route's resolution basis.
- `NULL_PRESERVING_HISTORY` now fires only for a dimension read through a hop into a validity
  window, names the hop, and says how much of an executed answer sits in the empty group
  ("2 of 2 new teams (100%) are in the empty Plan group") on complete results only; a
  complete answer without one no longer carries it. Totals use visible outputs only and
  fall back to row counts when any grouping fans out. Limited, truncated or metric-filtered
  results retain the static warning. A NULL-rejecting filter through the hop says it leaves
  rows out, once per dimension. Classification follows SQL lowering's operator case and
  spacing; `IS NULL`, `=` / `IS` with NULL, null-safe comparisons and boolean `IS` / `IS NOT`
  carry no such warning.
- Route meanings read a hop into a validity window as "the … valid at the time" instead of
  "any of the …".
