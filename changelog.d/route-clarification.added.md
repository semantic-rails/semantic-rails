- Query IR `route_decisions`: a query can answer an ambiguous route with the option the person
  chose, for that query only. Each row is shaped like a `graph.path_preferences` row (its `label`
  is ignored), is checked by the loader's rules, and applies before the package's row for the same
  exact pair, never to other pairs and never cached as the package's route. A bad row, two rows for
  one pair, or a row for a pair the query never walks is `INVALID_QUERY`; under a row filter on
  any entity of the pair's routes it is `POLICY_DENIED` (`route_override_under_row_policy`). Each
  applied row is disclosed as an `info` warning `ROUTE_CHOSEN_BY_QUERY` with the row and
  `replaced`, what would have applied without it. `build-options` follows the rows too. See
  [`route_decisions`](docs/QUERY_IR_SCHEMA.md#route_decisions).
- A `graph.path_preferences` row takes an optional `label`, the route's meaning in business words.
  The loader and the package writer keep it, and a compile's `hop_profile` and discovery's path
  availability show it as `route_label` for the recorded route.
- Architect `record_route_decision(source_entity, target_entity, relationship_path, label)`
  writes or replaces a pair's `graph.path_preferences` row, checked by the loader's rules (an
  off-route path is `INVALID_CONFIG` and writes nothing), and returns the replaced row and a
  one-sentence `summary` for the review. It is on the Architect MCP server and
  `ArchitectProject`. See [the Architect MCP](docs/ARCHITECT_MCP.md).
