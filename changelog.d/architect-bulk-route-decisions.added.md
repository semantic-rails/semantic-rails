- Architect can add a relationship with `keep_existing_routes: true` to record and
  preserve existing routes in the same transaction. `record_route_decision` also
  accepts a `decisions` list, validating and applying all pair decisions atomically.
