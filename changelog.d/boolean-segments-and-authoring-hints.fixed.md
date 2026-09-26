- Segments on a true/false column work from `author segment` to preview. `author model`
  declares a BOOLEAN column as `kind: boolean`, not `categorical`; the segment wizard
  offers true and false for it, types every other value by its dimension (`'completed'`
  means the text completed), asks again for a value of the wrong type, offers only
  entities that can hold a segment and that entity's own metrics, and starts an edit from
  the saved membership field, so Enter at every prompt keeps a segment it wrote.
- A segment or query filter value of the wrong type for its dimension fails validation
  with a recovery hint, and a segment preview the warehouse refuses carries a hint about
  membership values and dimension kinds.
- `author model` on a package whose seed files aren't built into its DuckDB file yet
  names `validate runtime`, which builds it, instead of `dbt build` (a dbt-built package
  still names `dbt build`).
- The bundled sample package, installed from a wheel, builds its DuckDB file in
  `~/.semantic_rails/cache/` (or under `SEMANTIC_RAILS_HOME`), one per installed version,
  not in `site-packages`.
  A `site-packages/data/jaffle_shop.duckdb` left by an earlier version can be deleted.
- REPL polish: `author model` recommends the largest unmodeled table and doesn't
  pre-tick rank or sequence-number columns as summed measures (the Architect's
  `suggest_model` marks them low confidence); "Model to extend" lists calendars last;
  the filtered-metric wizard offers true and false for a boolean dimension; the growth
  recipe's example question plans without a warning, and labels keep MoM, YoY and YTD;
  `help` lists `help [command]`; `ask` prints its warnings before the rows; and
  day-or-coarser time buckets print as dates.
