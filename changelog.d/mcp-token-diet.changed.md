- The query MCP costs a model less context. Tool descriptions and schema prose are shorter,
  with the same tools, arguments, enums and defaults; responses leave out empty optional fields
  and repeats of an error's code or message; `discover` cards leave out their bucket's `kind`
  and `available: true`; and `discover` with `verbosity="compact"` now returns slim cards plus
  each card's root entity, up to three match reasons and its starter patch
  (`verbosity="full"` returns the whole cards). The server instructions and `execute`'s
  description now name every variant Query IR composes at query time (aggregation overrides,
  filtered aggregates, rolling windows, prior-period offsets, period-to-date, cumulative,
  ratios, conversion windows, `scoped_aggregate`, `aggregate_if` and `metric_filters`), with
  their shapes on `execute`. `discover`'s `kinds` also accepts a JSON array sent as a string,
  and a select item written without its `expression` wrapper gets an error naming the shape.
