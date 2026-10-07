- In strict packages, unpublished measures are omitted from `discover` and held by `plan`
  unless the caller selects them by id in `partial_query.select`; no other part of the request
  names a measure. Explicit measure queries, published metrics, and non-strict packages
  keep their existing behavior.
