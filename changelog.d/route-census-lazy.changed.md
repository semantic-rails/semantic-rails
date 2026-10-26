- `validate`, `check` and Architect's `project_status` warn about a route only where the
  package's own metrics, examples or tests walk it. On a large warehouse with thousands of
  entity pairs that have two or more routes, `ROUTES_UNDECIDED` now counts only the pairs those
  queries need, and `ROUTE_PASS_THROUGH` only the crossings they read; `route_census` still
  lists every pair, each with `needed_by`, needed pairs first. Any other pair is asked about
  when a question needs it, never guessed, and an answer that reads a crossing still discloses
  it. A synthetic 74-entity campus warehouse goes from 3,199 undecided pairs and 690 crossing
  warnings to the 8 forks its 12 example questions hit.
- `plan` asks which route a question means when its draft is refused only because its pieces
  connect by two or more routes: `needs_clarification` with `AMBIGUOUS_PATH`, the question in
  business words ("Which District does the question mean for an Account: the District of the
  Account's Branch, or the District of the Account's Owner?"), and per reading a ready call and
  the `graph.path_preferences` row that makes it the package default. It used to return
  `low_confidence` with no next step.
