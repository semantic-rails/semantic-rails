- Segments on a true/false column work from `author segment` to preview. `author model`
  declares a BOOLEAN column as `kind: boolean`, not `categorical`; the segment wizard
  offers true and false for it, types every other value by its dimension (`'completed'`
  means the text completed), asks again for a value of the wrong type, and offers only
  entities with a metric and that entity's own metrics.
- A segment or query filter value of the wrong type for its dimension fails validation
  with a recovery hint, and a segment preview the warehouse rejects says to check the
  membership values against their columns instead of returning no hint.
- `author model` on a package whose seed files aren't built into its DuckDB file yet
  names `validate runtime`, which builds it, instead of `dbt build`.
- The bundled sample package, installed from a wheel, builds its DuckDB file in
  `~/.semantic_rails/cache/` (or under `SEMANTIC_RAILS_HOME`), not in `site-packages`.
- REPL polish: `author model` recommends the largest unmodeled table and doesn't
  pre-tick rank or sequence-number columns as summed measures; "Model to extend" lists
  calendars last; the filtered-metric wizard offers true and false for a boolean
  dimension; the growth recipe's example question plans without a warning, and labels
  keep MoM, YoY and YTD; `help` lists `help [command]`; `ask` prints its warnings before
  the rows; and day-or-coarser time buckets print as dates.
