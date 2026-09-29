- A measure filtered by a dimension across a one-to-many hop (order revenue from orders
  with a beverage item) now compiles instead of returning `MIXED_GRAIN_INVALID`: its leaf
  keeps one row per (entity key, output grain) before it aggregates, so each order counts
  once, however many matching items it has (EXISTS). A distinct count grouped by such a
  dimension (orders per item product type) counts each order once in every type it
  contains. A `REWRITE_APPLIED` warning (`fanout_dedup`) states both meanings. The path must
  go down one-to-many hops, each joined on the declared key of its one side, before any
  lookup; no package change is needed. Grouping another aggregation across the hop (order
  revenue by item product type), negated, null or `false` tests across it, and many-to-many
  or off-key paths stay refused, and `why_invalid` says why. A measure's leaf refusals are
  now all `MIXED_GRAIN_INVALID` (some were `REWRITE_NOT_SUPPORTED`), with recovery hints.
