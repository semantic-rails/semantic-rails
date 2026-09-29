- In a package whose one-to-many relationships are `rollup_safe`, a distinct count grouped
  by two dimensions that each cross a one-to-many hop, on the same child (orders by item
  product type and item product name) or on different children (customers by item product
  type and session store), used to be answered and now returns `MIXED_GRAIN_INVALID`: two
  groups that cross a one-to-many hop, on the same child or on different children, now
  refuse; ask one such group per query. A measure's leaf refusals are now all
  `MIXED_GRAIN_INVALID` (some were `REWRITE_NOT_SUPPORTED`), with recovery hints.
