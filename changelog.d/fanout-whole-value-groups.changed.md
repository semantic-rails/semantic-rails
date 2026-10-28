- A count of an entity's rows grouped by a dimension across a one-to-many hop (departments by
  course level, a row count by product type) is answered instead of refused with
  `MIXED_GRAIN_INVALID`: each row counts once in every group it has a child row in, as a
  distinct count already did. So is a sum or average of a value, with each row's whole value,
  while nothing across the hop carries a value of its own (order revenue by tag). The groups can
  overlap; `REWRITE_APPLIED` names the hop and says so (`details.overlapping_groups`). Where the
  child carries values (order revenue by item product type, beside item revenue), and for a
  median or other statistic, a non-additive measure or an item that sets the measure against
  one of another entity, the call stays refused with the same message as before. See
  [Groups across a one-to-many hop](docs/QUERY_CALL.md#groups-across-a-one-to-many-hop).
