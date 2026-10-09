- **Breaking (0.x):** the default validation profile is gone, and `package.schema_strict` is
  refused: every package is loaded with one set of authoring rules, in every layout. Forms
  only the default profile accepted are refused with `INVALID_CONFIG` and a hint naming the
  current form: a model `grain:`, `joins:`, `keys.foreign:`, `keys.primary:` beside
  `entities:`, a singular `entity:` without `entities:`, `id:` on graph entities,
  dimensions and measures (use `as:`), a model `id:` that differs from its key, `topics:`,
  a measure's `preferred_companion_metrics`, a measure without `kind:`, a metric without
  `value_type:`, and `relations:` in a directory package's `package.yml`. Rows finer than
  their entity are modelled as an entity of their own, keyed by the row and related to its
  parent. See
  [Refused legacy forms](docs/PACKAGE_AUTHORING.md#refused-legacy-forms).
- **Breaking (0.x):** measures never publish a metric of their own name. A measure stays
  queryable as a measure; author a metric under `metrics:` to publish one. A measure's
  `publish:` is `true` or `false` only (`publish: false` still marks an unoffered measure).
  Measure cards no longer carry `default_metric_id`, metric cards no longer carry
  `curated_status` or `aggregation_mode`, and `discover` no longer lowers a metric whose id
  matches a measure's name. Architect `upsert_model` no longer takes `joins`; relationships
  go through `upsert_relationship`.
- `semantic-rails project upgrade` rewrites a package written for an earlier release with six
  new rules: `package-schema-strict`, `measure-auto-publish`, `model-grain`,
  `model-primary-key`, `model-joins` and `object-as`.
