- `semantic-rails ls` accepts the REPL's `ls [kind] [search]` form, for example
  `semantic-rails ls metric revenue`.
- `author model` warns when the seed files changed after the DuckDB file was built, with
  the command that rebuilds it, and refuses a typed table name the file lacks.
- `author model` no longer pre-ticks `_cents` columns as money amounts, which printed cents
  as dollars.
- The authoring banner says that typing `cancel` in a list picks an option containing it.
