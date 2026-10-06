- Architect can add a relationship with `keep_existing_routes: true` to record and
  preserve existing routes in the same transaction, allowing new answers from the
  relationship itself while refusing changes to previously refused pairs caused
  by generated decisions. The keep retry hint appears only for relationship writes. `record_route_decision` also
  accepts a `decisions` list, validating and applying all pair decisions atomically.
