- The compile plan's `performance_plan.aggregate_routing.candidates` lists every
  declared rollup considered for each measure leaf, whether it was `selected`,
  `eligible`, `rejected` or `unknown`, and why. Setting
  `SEMANTIC_RAILS_AGGREGATE_ROUTING=off` (or calling
  `runtime.set_aggregate_routing(False)`) runs every query a runtime serves on the
  base tables, including queries whose compiled plan is already cached.
- `semantic_rails.cache.compilation_cache_key` takes a required `aggregate_routing`
  argument, so a custom compile cache keys on the routing switch. Plans cached
  before the upgrade miss once.
