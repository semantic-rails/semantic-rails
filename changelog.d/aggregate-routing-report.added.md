- The compile plan's `performance_plan.aggregate_routing.candidates` lists every
  declared rollup considered for each measure leaf, whether it was `selected`,
  `eligible` or `rejected`, and why. Setting `SEMANTIC_RAILS_AGGREGATE_ROUTING=off`
  (or calling `runtime.set_aggregate_routing(False)`) runs every query on the base
  tables, including queries whose compiled plan is already cached.
