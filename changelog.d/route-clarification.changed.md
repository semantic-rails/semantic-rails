- An `AMBIGUOUS_PATH` refusal now asks which route the question means, in business words:
  `details.clarification` replaces `details.candidates`, `details.meanings`, `details.pins` and
  `details.conflicts_with`. It holds the question ("Which District does the question mean for an
  Account?") and one option per route the route rule keeps: a `meaning` built only from package
  labels ("the District of the Account's Branch"; a one-to-many hop reads "any of the …", and two
  relationships between the same entities are told apart by their own label or their foreign-key
  columns), an `id` unique within the refusal (`branch_district`, `origin_airport`), the route's
  `relationship_path`, and its `decision`: the `graph.path_preferences` row that makes it the
  package default. When that row would disagree with the package's rows, the option adds
  `conflicts_with`, the rows to change before recording it; its `decision` still answers per
  query. Every refusal of an ambiguous route, a conditional aggregate's included, carries the same
  clarification. `details.start`, `details.target` and `details.hint` stay. See
  [the route rule](docs/PACKAGE_AUTHORING.md#the-route-rule).
- The MCP `execute` tool tells agents to ask, then resend the chosen option's `decision` in
  `route_decisions`.
