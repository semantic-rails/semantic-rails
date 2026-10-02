- Query IR `route_decisions`: a query can answer an ambiguous route with the option the person
  chose, for that query only. Each row is shaped like a `graph.path_preferences` row (its `label`
  is ignored), must take one of the pair's routes within the hop ceiling (else `INVALID_QUERY`,
  `route_not_offered`), is checked by the loader's rules, and applies before the package's row for
  the same exact pair, never to other pairs and never cached as the package's route. A bad row,
  two rows for one pair, or a row for a pair the query never walks is `INVALID_QUERY`; under a row
  filter on any entity of the pair's routes it is `POLICY_DENIED`
  (`route_override_under_row_policy`). Each applied row is disclosed, at every verbosity, as an
  `info` warning `ROUTE_CHOSEN_BY_QUERY` with the row and `replaced`, how the package resolves the
  pair without it (`decided`, `colocated_key`, `inherited`, `only_route` or `undecided`), and
  `hop_profile` reports `route_basis: query`. `build-options` follows the rows (a patch that would
  leave a row unused is offered blocked with that refusal), and live `valid-values` reads values
  through them. See [`route_decisions`](docs/QUERY_IR_SCHEMA.md#route_decisions).
- A `graph.path_preferences` row takes an optional `label`, the route's meaning in business words.
  The loader and the package writer keep it, and a compile's `hop_profile` and discovery's path
  availability show it as `route_label` for the recorded route.
- Architect `record_route_decision(source_entity, target_entity, relationship_path, label)`
  records a pair's route in the `path_preferences` list the loader reads, replacing every row for
  exactly the pair. The row is checked by the loader's rules, and the changed package is loaded
  before anything is written: an off-route path, a row another row disagrees with (named in
  `details.rows`), or a row that would not take effect is `INVALID_CONFIG` and writes nothing. It
  returns the row in effect before (`replaced`) and a one-sentence `summary` for the review. It is
  on the Architect MCP server and `ArchitectProject`. See
  [the Architect MCP](docs/ARCHITECT_MCP.md).
