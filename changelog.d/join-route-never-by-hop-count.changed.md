- The engine never chooses a join route by hop count or weight. Which of two routes to an entity a
  question means is a business definition: the package records it once as a
  `graph.path_preferences` row, and every query uses it. For each start and target entity, over
  every route within the hop ceiling: a query's own `route_decisions` row for exactly the pair
  wins, for that query only; then a package row for exactly the pair; then the start entity's one
  direct key (a many-to-one or one-to-one relationship from it); then the routes that follow every
  row whose pair they walk through, when one remains. Anything else is refused with
  `AMBIGUOUS_PATH`, whatever the routes' lengths: two direct keys, routes with no direct key, and
  routes that all fan out, where the shortest used to win. The refusal is a clarification:
  `details.reason` is `route_decision_required`, and `details.clarification` asks which route the
  question means, one option per route with its meaning in business words and the row that
  decides it (rows accept entity ids as well as keys and names). One place resolves every route
  (grouping, filters, a measure's own filter, metric predicates, time roles, conversions, the
  direct read of a foreign key, grain recovery hints and discovery), and it remembers a refusal as
  it remembers a route. So adding a route never changes an answer silently: a pair answered by its
  own key keeps the answer, and any other pair is refused until a row records it. See
  [the route rule](docs/PACKAGE_AUTHORING.md#the-route-rule).
- `PATH_ALTERNATES_UNPINNED` is replaced by two short `info` notes, at `compact` and `full`
  verbosity: where the engine chose one of two or more routes for a pair the query reads,
  `ROUTE_COLOCATED_KEY` (the start entity's own key) or `ROUTE_RECORDED` (a
  `graph.path_preferences` row) names the chosen route (`details.route`, and its meaning in the
  message). A pair with one route gets none, and the minimal response, the MCP default, leaves
  them out.
- A calendar dimension reached only through other facts' rows (orders grouped by a calendar month
  through store inventory snapshots) is now refused with `AMBIGUOUS_PATH` instead of
  `MIXED_GRAIN_INVALID`; its recovery hint still points at `time.grain`.
- `jaffle_shop` records four routes (an item's customer and store through its order, and the stores
  and products a customer ordered) and the comparison package two (an item's customer and store
  through its order); an order's, a lifecycle event's or a session's own customer and store keys
  need no row. Every answer is unchanged. The package writer writes `graph.path_preferences`, and
  an Architect removal drops, and lists, the rows that name an entity or relationship it removes.
- Packages imported or converted from other tools may need `graph.path_preferences` rows: a
  MetricFlow project with denormalized foreign keys, or a package exported to Ossie and back (the
  export doesn't carry the rows), can have pairs that were answered by their shortest route and
  are now refused until a row records the route.
