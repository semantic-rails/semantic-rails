- Re-importing a dbt model with the Architect's `import_dbt_project` no longer reverts the
  dimensions, times and measures an author changed on the package model. The import restated each existing dimension, time and
  measure from the dbt draft, so a `stock` measure turned back into a summed `flow`, an
  `as_of_time` clock back into `event_time` (both unreported) and a declared `additive: false`
  was dropped. An import now adds only the objects the model doesn't have yet, leaves existing
  ones as authored and lists them in each model's `kept_objects`; it no longer refreshes an
  existing object's dbt description or value set (change it with `upsert_model`). The model's
  relation, its entity's key and its foreign-key entries still follow dbt.
