- An answer whose only route goes through another table's rows and up to another parent
  ("the Team of any of the Member's Member events") now carries a `ROUTE_PASS_THROUGH`
  warning at every verbosity: a member with no event is left out and one with several can
  count under several teams. It names the fixes: declare the key, declare the table a link
  table with `bridge: true` (now kept on the entity), record the route, or ask with a child
  group. Answers and routes are unchanged.
- `NULL_PRESERVING_HISTORY` now fires only for a dimension read through a hop into a validity
  window, names the hop, and says how much of an executed answer sits in the empty group
  ("2 of 2 new teams (100%) are in the empty Plan group"); a complete answer without one no
  longer carries it, and a filter through the hop says it leaves rows out.
- Route meanings read a hop into a validity window as "the … valid at the time" instead of
  "any of the …".
