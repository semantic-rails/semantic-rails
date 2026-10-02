- A join route is chosen only by a recorded decision or the start entity's own key. For each
  start and target entity: a `graph.path_preferences` row for exactly the pair wins; otherwise
  the start's one direct key to the target is used, even where a row for another pair points
  elsewhere (a loan that holds its own district reads it, noted `ROUTE_COLOCATED_KEY`);
  otherwise every row holds wherever a route walks its pair, so a row for (account, district)
  also decides the district of a loan, card or transaction reached through the account, the
  region beyond the district, and, walked back, the accounts of a district. The one route left
  is used; two or more are refused with `AMBIGUOUS_PATH`, whatever their lengths. See
  [the route rule](docs/PACKAGE_AUTHORING.md#the-route-rule).
- When the rows rule out every route within the hop ceiling, the query is refused with
  `PATH_NOT_FOUND` and `details.reason: excluded_by_decision`, naming the rows in
  `details.rows`.
- `graph.path_preferences` rows must agree: when one row's path walks through another row's
  pair by a different route (or the reverse pair records another route), the package fails to
  load with `INVALID_CONFIG`, naming both rows in `details.rows`.
- `ROUTE_COLOCATED_KEY` notes list, in `details.alternatives`, the row that would make each
  other route the default. A route inherited from rows is noted `ROUTE_RECORDED`, with the rows
  it follows in `details.rows`. `hop_profile` targets carry `route_basis`: `decided`,
  `colocated_key`, `inherited` or `only_route`.
- A distinct count computed from a child's rows (customers counted from their orders) reads
  each grouping through the counted entity's own route. A customer's city read through its own
  key beside its region recorded through the orders' ship-to city is now refused with
  `PATH_JOIN_CONFLICT`; it used to read both from the ship-to city.
- `jaffle_shop` and the comparison package keep every answer. Pairs they refused because two
  routes reached the target now follow their recorded rows where those leave one route (for
  example, the items of a customer's orders).
