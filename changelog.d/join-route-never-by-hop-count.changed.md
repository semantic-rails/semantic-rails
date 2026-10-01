- The engine never chooses a join route by hop count. Which of two routes to an entity a question
  means is a business definition: the package records it once as a `graph.path_preferences` row,
  and every query uses it. For each start and target entity, a row wins; otherwise the only route
  is used; otherwise, when exactly one route is the start entity's own direct key (a many-to-one or
  one-to-one relationship from it), that key is used and the response says so with
  `PATH_ALTERNATES_UNPINNED`, naming every other route and the row that would record each.
  Anything else is refused with `AMBIGUOUS_PATH`, whatever the routes' lengths: two direct keys,
  routes with no direct key, and routes that all fan out, where the shortest used to win. The
  refusal is a clarification: `details.reason` is `route_decision_required`, `details.meanings`
  reads each route as a chain of labels ("Account → Owner → Home region"), and `details.pins` holds
  the row that records each (rows accept entity ids as well as keys and names). One place resolves
  every route (grouping, filters, a measure's own filter, metric predicates, time roles,
  conversions, the direct read of a foreign key, grain recovery hints and discovery), and it
  remembers a refusal as it remembers a route. So adding a route never changes an answer
  silently: a pair answered by its own key keeps the answer and gains the warning, and any other
  pair is refused until a row records it. See
  [the route rule](docs/PACKAGE_AUTHORING.md#the-route-rule).
- A calendar dimension reached only through other facts' rows (orders grouped by a calendar month
  through store inventory snapshots) is now refused with `AMBIGUOUS_PATH` instead of
  `MIXED_GRAIN_INVALID`; its recovery hint still points at `time.grain`.
- `jaffle_shop` records four routes (an item's customer and store through its order, and the stores
  and products a customer ordered) and the comparison package two (an item's customer and store
  through its order); an order's, a lifecycle event's or a session's own customer and store keys
  need no row, and their answers now carry the warning. Every answer is unchanged. The package
  writer writes `graph.path_preferences`, and an Architect removal drops, and lists, the rows that
  name an entity or relationship it removes.
- Packages imported or converted from other tools may need `graph.path_preferences` rows: a
  MetricFlow project with denormalized foreign keys, or a package exported to Ossie and back (the
  export doesn't carry the rows), can have pairs that were answered by their shortest route and
  are now refused until a row records the route.
