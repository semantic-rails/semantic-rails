- A sum, count or average of an entity's own measure grouped by a dimension across a
  one-to-many hop is answered instead of refused with `MIXED_GRAIN_INVALID`: each row counts
  once, with its whole value, in every group it has a child row in ("revenue of the orders
  that included each product type"), as a distinct count already did. The groups can overlap;
  `REWRITE_APPLIED` names the hop and says so (`details.overlapping_groups`), and `plan`
  repeats it for a sum or average of a value. A median or other statistic, a non-additive
  measure, and an item that sets the measure against one of another entity stay refused, with
  the same message as before. See
  [Groups across a one-to-many hop](docs/QUERY_CALL.md#groups-across-a-one-to-many-hop).
