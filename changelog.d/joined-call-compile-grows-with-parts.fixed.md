- A joined call compiles in time and SQL that grow with its parts and their keys, plus one
  count per pair of parts (the `unmatched` counts of `PARTS_JOINED`), and answers as before.
  Each count read every part's key columns, so 200 parts of one metric by one key compiled
  385 MB of SQL in about 13 s; they now compile 5.6 MB in about a second. See
  [Calls side by side](docs/QUERY_CALL.md#calls-side-by-side-parts-and-join).
- On Postgres, which reads at most 1,664 columns in one select, a joined call wider than that
  is refused before it is sent (`INVALID_QUERY`, `details.reason`: `parts_over_column_limit`),
  saying how many columns it needs and how many of its parts fit, instead of failing in the
  warehouse with a generic execution error. 53 parts of one metric by one key fit.
