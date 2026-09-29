- In a package whose one-to-many relationship is `rollup_safe`, a distinct count grouped by
  two dimensions across the same one-to-many hop (orders by item product type and item
  product name) used to be answered and now returns `MIXED_GRAIN_INVALID`: a query may group
  or filter across a one-to-many hop once, so ask one such group per query. A measure's leaf
  refusals are now all `MIXED_GRAIN_INVALID` (some were `REWRITE_NOT_SUPPORTED`), with
  recovery hints.
