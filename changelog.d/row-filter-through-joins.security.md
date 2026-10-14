- Row filters now hold through joins, routes and entity hops. A filter restricts its own
  relation and every model whose package route reaches it through `many_to_one` owners
  (orders of a filtered store, lines of those orders), and each caller's answers are the
  answers on their visible rows: lookups, groups by an owner's dimensions, child filters,
  `EXISTS`, metric predicates, shares and windows that were refused under a row filter are
  now answered. Every read of a filtered or covered relation carries its visibility
  condition, and a statement the engine can't prove that for is refused, never run
  unfiltered. Counting a model no filter reaches is refused unless the filter lists it in
  the new `shared:` key; a lookup along a route that isn't how the model is owned, a
  model whose ownership is undecided, relation pipelines over a filtered relation, and
  row-level calls that could fail on a value (anything but a short list of calls that
  can't, such as `COALESCE`, `LOWER` or a `CAST` to text) are refused under a filter. Declared value domains of filtered and covered models are read
  live under the filter, and `validate` lists each filter's covered, held and refused
  models. See [docs/PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md).
  Known limitations in this release: SQL output for a row-filtered caller can show the
  names of filter tables and columns, never values, so hosts that hide them should not grant
  SQL output to filtered callers; and on Snowflake, optional (`LEFT`) lookups into tables a
  filter covers are refused. Under a row filter, a filter value on a model no filter reaches
  isn't checked against the data, so such a query gets no "value not found" warning; this
  never changes a number. An empty window reports the data coverage of the caller's own
  rows.
