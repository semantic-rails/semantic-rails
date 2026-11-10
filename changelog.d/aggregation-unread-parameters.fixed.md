- An aggregation given a parameter it doesn't read is refused with `UNSUPPORTED_AGGREGATION`
  naming it, instead of answering without it. "Average title length" written as an `avg` of a
  row count with `"parameters": {"expression": "LENGTH(title)"}` answered `1.0`, the average of
  the count itself; an `avg` with a `p`, or a `percentile` with a second key, likewise answered
  as if the key were absent. Only `percentile` reads a parameter (`p`). The same holds for a
  `distribution`'s `p` and `parameters`, and for a metric that names a measure. To aggregate a
  row expression, write `{"kind": "aggregate_if", "aggregation": "avg", "value": <expression>}`.
  See [The aggregation override](docs/QUERY_CALL.md#the-aggregation-override).
