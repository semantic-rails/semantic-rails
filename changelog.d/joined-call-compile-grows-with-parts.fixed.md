- A joined call compiles in time and SQL that grow with its parts and their keys, plus one
  count per pair of parts (the `unmatched` counts of `PARTS_JOINED`), and answers as before.
  Each count read every part's key columns, so 200 parts of one metric by one key compiled
  385 MB of SQL in 12 s; they now compile 5.6 MB in about a second. See
  [Calls side by side](docs/QUERY_CALL.md#calls-side-by-side-parts-and-join).
