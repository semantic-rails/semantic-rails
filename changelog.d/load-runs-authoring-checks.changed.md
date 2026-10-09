- Loading a package (`serve`, MCP, the Architect, `Runtime.from_path`) now runs the same
  authoring checks as `validate-config`: an unknown key or an unknown kind, class or
  accumulation value fails loading with one `INVALID_CONFIG` listing every error, instead of
  being only a `validate-config` finding. `graph.relationships` entries, `semantic_caveats`
  rows, `defaults:` and the document top level now have closed key sets too, every rollup
  `columns:` binding is checked whatever its name resolves to, and a model's `defaults:`,
  which nothing read, is refused (a key starting with `_` stays an annotation). `relations`
  entries are not closed yet. In a directory package, a root key that a file's wrapper leaves
  out (a sibling of `graph:` in `graph.yml`, or a `defaults.yml` without `defaults:`) is
  refused instead of dropped. The package writer no longer writes `observation_scope` in
  the `package:` block, where the loader never read it; `project upgrade` removes it from
  existing packages with the `ignored-key` rule.
