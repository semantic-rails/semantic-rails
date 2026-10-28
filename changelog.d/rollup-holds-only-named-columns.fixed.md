- A query reads from a rollup (a `model.variants:` entry) only the measures and dimensions its
  `columns:` names. A measure or dimension it doesn't name used to be read from a column named
  after it, which the rollup table might not have, so the call failed with
  `QUERY_EXECUTION_ERROR`; such a query now reads the base tables. Name each column the rollup
  table holds in `columns:` for it to answer from the rollup, and `excludes:` the dimensions it
  isn't grouped by.
