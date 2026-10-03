- `semantic-rails check` and `validate` list the join routes a package still has to decide. The
  parse report's `route_census` names every entity pair a question can need (from any
  entity to each other reachable entity) that is refused with `AMBIGUOUS_PATH` until a
  `graph.path_preferences` row records its route (`undecided`, with the refusal's clarification
  options; pass an option's `decision` to `record_route_decision`), and
  the multi-route pairs answered by the start entity's own key (`assumed`, to confirm). One
  `ROUTES_UNDECIDED` warning counts them; the shipped `jaffle_shop` package has 18. Architect's
  `project_status` returns the census, and its next actions, like those of `create_project` and
  `setup_project_dialog`, say to decide the pairs; `promotion_check` lists them under
  `advisories`, never as a blocker.
- `impact-report` lists `route_changes`: each such pair a change resolves differently, such as a
  pair refused after a new relationship adds a second route, with its base and head route or
  refusal code, without suggesting recovery rows. Any entry makes the risk
  `high`, and the Markdown summary lists each one in entity labels. See
  [the route census](docs/PACKAGE_AUTHORING.md#route-census-and-route-changes).
- Architect writes and previews refuse unapproved changes to answered join routes with
  `ROUTE_DECISION_NOT_RECORDED`, listing affected pairs and explicit `graph.path_preferences`
  fields (`source_entity`, `target_entity`, `relationship_path`), without suggesting rows.
  Census, impact and guard comparisons use package decisions independently of active query
  route overrides. Authors record decisions with `record_route_decision` or include chosen rows
  in the change; Architect generates no route rows and `route_decisions_added` stays empty.
  An explicit route defines lookup semantics, including unmatched keys. Removals use the
  same preservation guard: a cut may leave a pair refused, while another answer requires
  its own decision. Deliberate decisions and removals report every changed pair in
  `route_changes`, including refused-to-answered and inherited changes. Writes and previews
  with `validate_after=False` refuse loader-invalid input when the current package loads,
  preserving the route baseline through subsequent edits.
