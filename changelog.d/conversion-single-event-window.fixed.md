- A conversion metric whose base and converted operands both count the conversion entity
  itself on the same clock (for example a 90-day repeat-purchase rate counting customers
  instead of orders) is now rejected by package validation and at query time. Each entity
  was a single event that converted to itself, so the window never applied and the metric
  returned the share of entities that ever matched the converted filter. The error names
  measures that count events keyed by the entity, such as orders.
