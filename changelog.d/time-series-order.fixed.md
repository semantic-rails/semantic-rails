- Time-series results default to ascending time order on every warehouse,
  including filled series, with grouped dimensions breaking ties in their stated
  order. This default applies only to the request's final projection, keeping
  internal branches and predicate sources unordered. Explicit ordering continues
  to take precedence.
