- An `AMBIGUOUS_PATH` refusal now asks which route the question means, in business words:
  `details.clarification` replaces `details.candidates`, `details.meanings` and `details.pins`. It
  holds the question ("Which District does the question mean for an Account?") and one option per
  route: a `meaning` built only from package labels ("the District of the Account's Branch"; a
  one-to-many hop reads "any of the …", and two relationships between the same entities are told
  apart by their own label or their foreign-key columns), an `id` unique within the refusal
  (`branch_district`, `origin_airport`), the route's `relationship_path`, and its `decision`: the
  `graph.path_preferences` row that makes it the package default, loadable as written. Every
  refusal of an ambiguous route, a conditional aggregate's included, carries the same
  clarification, and a filter on a child entity reached by more than one route asks the same way.
  `details.start`, `details.target` and `details.hint` stay. See
  [the route rule](docs/PACKAGE_AUTHORING.md#the-route-rule).
- The MCP `execute` tool tells agents to ask, then resend the chosen option's `decision` in
  `route_decisions`.
