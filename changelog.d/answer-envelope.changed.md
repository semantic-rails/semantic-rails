- An answer names each route chosen among several in `routes` (`{from, to, via}` at every
  verbosity; compact and full responses add how it was chosen), replacing the
  `ROUTE_COLOCATED_KEY`, `ROUTE_RECORDED` and `ROUTE_CHOSEN_BY_QUERY` notes. `assumptions`
  are objects with a code (`WINDOW_TOTAL`, `PARTIAL_PERIOD`, `ENTITY_FILTER_OVER`), and
  `warnings`, `assumptions`, `errors` and other lists appear only when they have entries.
  Verbosity no longer changes which warnings exist: every matching caveat is stated and
  `SEMANTIC_CAVEATS_TRUNCATED` is gone. The echoed `query` and `normalized_query` are the
  call, without identity or `now`. Error and warning objects leave out their empty fields
  too, on every transport; `code` and `message` always stay.
