- `semantic-rails check` and `validate` list the join routes a package still has to decide. The
  parse report's `route_census` names every entity pair a question can need (from a measure's
  entity to another entity with a dimension) that is refused with `AMBIGUOUS_PATH` until a
  `graph.path_preferences` row records its route (`undecided`, with the refusal's details), and
  the pairs answered by the start entity's own key (`assumed`, to confirm). One
  `ROUTES_UNDECIDED` warning counts them; the shipped `jaffle_shop` package has 21. Architect's
  `project_status` returns the census, and its next actions, like those of `create_project` and
  `setup_project_dialog`, say to decide the pairs; `promotion_check` lists them under
  `advisories`, never as a blocker.
- `impact-report` lists `route_changes`: each such pair a change resolves differently, such as a
  pair refused after a new relationship adds a second route, with its base and head route or
  refusal code and the `keep_base` row that keeps the earlier route. Any entry makes the risk
  `high`, and the Markdown summary lists each one in entity labels. See
  [the route census](docs/PACKAGE_AUTHORING.md#route-census-and-route-changes).
- Architect writes keep the answers a package already gives. When a change would refuse a pair,
  or answer it by another route, the same change records the earlier route as the pair's
  `graph.path_preferences` row, and the result lists it in `route_decisions_added` with the new
  routes. A change that writes the pair's own row is left as written, and a removal lists the
  answers it moves in `route_changes`. If an added row would not take effect, the change is
  refused with `ROUTE_DECISION_NOT_RECORDED` and nothing is written.
