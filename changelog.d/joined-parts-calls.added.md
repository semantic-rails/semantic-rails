- A query call can now be several calls answered side by side, each with its own `where`:
  `{"parts": [call, ...], "join": {"on": [[dimension of each part], ...], "keep": ...}}`, with
  one `time` block, `order_by` and `limit` for the whole. Each part is answered as the call it
  is (the caller's view, policies, row filters, routes, display keys and every disclosure,
  tagged with its part), and the parts are joined on their keys in one statement: a key only
  another part has reads 0 or NULL as an empty group does, a NULL key stays its own row, and
  `join.keep` keeps only the keys of the parts it names. Parts join on the same dimension or on
  the two columns a declared relationship joins. `PARTS_JOINED` names the keys and counts each
  part's rows, NULL-key rows and unmatched keys over the whole result. A part with `having`, a
  part grouped finer than the join keys and a part with no clock beside a timed call are
  refused. See [Calls side by side](docs/QUERY_CALL.md#calls-side-by-side-parts-and-join).
