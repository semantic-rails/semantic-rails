- A metric whose expression carries a part the loader cannot keep is now rejected at load,
  naming the metric and the part, instead of loading as a broader metric. A `scoped_aggregate`
  recipe with an `anchor` and `window` used to return a lifetime value; it now keeps them and
  is refused when queried until anchored windows compile. The `INVALID_ANCHOR_ROLE` hint no
  longer points authors at a metric recipe and suggests an offset column instead.
