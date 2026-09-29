- A measure filtered by a dimension across a one-to-many hop (order revenue from orders
  with a beverage item) now compiles instead of returning `MIXED_GRAIN_INVALID`: its leaf
  keeps one row per (entity key, output grain) before it aggregates, so each order counts
  once, however many matching items it has. A distinct count grouped by such a dimension
  (orders per item product type) counts each order once in every type it contains. The
  path must go down one-to-many hops before any lookup and the measure's entity needs its
  key; relationship metadata already carries both, so no package change is needed.
  Grouping another aggregation across the hop (order revenue by item product type), a
  negated filter across it, and many-to-many paths stay refused, and `why_invalid` says why.
