- `additive: false` on an `aggregate` measure declares values that are already aggregated, such
  as a vendor's pre-counted unique visitors, which the engine must never add together. A query
  that would sum more than one of its rows (for a stock, more than one series) into an output row
  is refused with `ROLLUP_UNSAFE` and `details.unsupported_construct: non_additive_sum`, pointing
  to the measure's key; cumulative, rolling and period-to-date metrics over it are refused, and
  `avg`, `min`, `max`, `median`, `percentile` and `prior_period` stay available. `ROLLUP_UNSAFE`
  previously meant only a parent-entity rollup; check `unsupported_construct` to tell them apart.
  The new field is part of each measure's semantic payload, so every package's
  `semantic_fingerprint` changes once on upgrade.
