- A measure filtered by a dimension across a one-to-many hop (order revenue from orders
  with a beverage item) now compiles instead of returning `MIXED_GRAIN_INVALID`: its leaf
  keeps one row per (entity key, output grain) before it aggregates, so each order counts
  once, however many matching items it has (EXISTS). A distinct count grouped by such a
  dimension (orders per item product type) counts each order once in every type it
  contains. A `REWRITE_APPLIED` warning (`fanout_dedup`) states both meanings. The path must
  go down one-to-many hops, each joined on the declared key of its one side, before any
  lookup; no package change is needed. The new leaf does not answer, and `why_invalid` says
  why: another aggregation grouped across the hop (order revenue by item product type),
  negated, null or `false` tests across it, a second group or filter across a one-to-many
  hop (a query may have one), measures whose rows are finer than their entity's key, and
  many-to-many or off-key paths. Every leaf that crosses such a hop, including the
  `entity_in_terms_of` rewrite in a query with several measures, now carries a rewrite step.
