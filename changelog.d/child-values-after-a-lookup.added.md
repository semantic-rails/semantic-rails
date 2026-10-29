- A call grouped by a child's column reached by lookups, then one one-to-many hop (rooms by the
  cities of their building's addresses, sessions by the product types of their order's items)
  now answers where the route is decided, instead of being refused with `MIXED_GRAIN_INVALID`.
  Each stored row counts once, with its whole value, in every group of values its parent's child
  rows hold (two child rows with the same values count once), a row whose parent has no child
  row, or that names no parent, is the NULL group, and a total is the call without the grouping.
  `REWRITE_APPLIED` (`child_values`) says the groups overlap and don't add up to the total. A
  median, a non-additive measure, a conditional aggregate, a share, a filter on the child, a call
  with `time` or `having`, and any such call on ClickHouse (which joins lookups inner) keep their
  refusal. See
  [Groups by a child's values after a lookup](docs/QUERY_CALL.md#groups-by-a-childs-values-after-a-lookup).
