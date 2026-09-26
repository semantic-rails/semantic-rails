- Rollup bindings are checked when a package loads. A measure binding (a variant
  `columns:` entry or an `aggregate_relations:` measure) accepts only `column`,
  `rollup`, `aggregation` and `holds`, and a dimension binding only `column` and
  `path`. Any other key, a `holds` the measure can't be queried with, or an
  unknown relationship in `path` is now `INVALID_CONFIG`, where before it was
  ignored. An `aggregate_relations:` entry that holds a column from another model
  without a many-to-one `path` no longer routes at all.
  `performance_plan.aggregate_routing.selected_count` now counts the distinct
  rollups the SQL reads, not the rollup scans in the physical plan.
