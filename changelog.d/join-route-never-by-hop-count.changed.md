- The engine no longer chooses a join route by hop count or `path_preference` weights when the
  routes can mean different things. With a functional route (every hop many-to-one or
  one-to-one), the eligible routes are the functional ones and any one-to-many route no longer
  than the shortest functional one; one eligible route is used, routes of one length keep the
  weight tie-break, and routes of different lengths are refused with `AMBIGUOUS_PATH`. The refusal
  lists every route in `details.candidates` and the `graph.path_preferences` row that pins each in
  `details.pins`; those rows load as written, since a pin now accepts entity ids as well as keys
  and names. "Accounts by region" through an account's branch region beside its owner's home
  region was answered by the branch route, even with weights favouring the other; it is now
  refused until a pin says which. One place resolves every route (grouping, filters, a measure's
  own filter, metric predicates, time roles, conversions, the direct read of a foreign key, grain
  recovery hints and discovery), so discovery follows a pin as compilation does.
- `PATH_ALTERNATES_UNPINNED` now warns only when every route crosses a one-to-many hop and no pin
  covers the pair, whatever the weights say; a functional route beside longer one-to-many routes
  answers without it. See [the route rule](docs/PACKAGE_AUTHORING.md#the-route-rule).
- `jaffle_shop` and the comparison package pin the routes they already took (8 and 7 pairs), so
  their answers are unchanged. The package writer now writes `graph.path_preferences`, and an
  Architect removal drops the pins that name what it removes.
