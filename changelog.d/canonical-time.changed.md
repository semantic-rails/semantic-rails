- Declare a model's default time only with `times.<key>.default: true`; multiple defaults
  on one model and the old `default_time` / `default_query_axis` keys are refused.
  `project upgrade` removes old axis hints with the certified `time-default-axis` rule.
- Refuse `clock_variants`, `comparison_peers` and `preferred_filter_ops` in packages
  (including measure `publish:` mappings) and omit these advisory fields from catalog
  and discovery cards. `defaults.time.default` is refused: declare the default on its role.
- Package writes preserve each measure's clock; ambiguous default time placement refuses
  instead of giving an untimed measure a clock.
