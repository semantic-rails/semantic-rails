- An entity's `inspect` card lists the relationships it takes part in, seen from its side (the
  other entity, cardinality, the directions a route may walk, the join columns), and the
  entities a query rooted on it reaches within the hop ceiling, each with its route, marked
  `recorded` when the package's recorded route rows chose it or `route_decision_required` when
  the call has to choose one. A minimal card keeps the first 10 of each without the routes.
  Hidden relationships and entities never appear. `discover` entity cards carry the
  relationships at `verbosity: "full"`.
- `discover` ranks relationships when `kinds` names `relationship`, with each one's entities,
  cardinality, directions and join columns.
