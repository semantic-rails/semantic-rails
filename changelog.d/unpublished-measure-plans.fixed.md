- In strict packages, unpublished measures are omitted from `discover` and held by `plan`
  unless the caller names them by id in Query IR fields of `partial_query`; request metadata
  never names a measure. Explicit measure queries, published metrics, and non-strict packages
  keep their existing behavior.
