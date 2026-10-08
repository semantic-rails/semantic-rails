- Loading a package (`serve`, MCP, the Architect, `Runtime.from_path`) now runs the same
  authoring checks as `validate-config`: an unknown key or an unknown kind, class or
  accumulation value fails loading with one `INVALID_CONFIG` listing every error, instead of
  being only a `validate-config` finding. `graph.relationships` entries, `semantic_caveats`
  rows, `defaults:` and the document top level now have closed key sets too (a key starting
  with `_` stays an annotation). The package writer no longer writes `observation_scope` in
  the `package:` block, where the loader never read it; `project upgrade` removes it from
  existing packages with the `ignored-key` rule.
