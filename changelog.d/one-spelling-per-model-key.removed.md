- **Breaking (0.x):** each model, measure and object key has one spelling; the other is
  refused with `INVALID_CONFIG` and a hint naming the current one. Refused: a measure's
  `time:` (use `times: [<role>]`); `id:` on graph relationships, times, metrics and segments,
  as already on graph entities, dimensions and measures (use `as:`; a model's `id:` stays its
  identity); a model's `keys:` and singular `entity:` (bind the model with
  `graph.entities.<x>.model` and list its entities under `entities:`; a fact model keeps
  `keys.primary:` as its row key); a dimension's `valid_values:` (use `domain:`);
  `freshness_source`, `freshness_sla_seconds` and `freshness_as_of` on a graph entity (declare
  them on its model); `from_` and `label` in a measure's `validity_windows` and
  `external_discontinuities` rows, which now take only `from`, `to` and `semantics`, or
  `from`, `to`, `what` and `magnitude_estimate_pct`; a measure's `aggregation:`, which was
  never read (use `default_agg:`); and `topics:` on models, graph entities and times, as
  already on dimensions, measures, metrics and segments. See
  [Refused legacy forms](docs/PACKAGE_AUTHORING.md#refused-legacy-forms).
- `semantic-rails project upgrade` rewrites a measure's `time:` to `times:` with the new
  `measure-times` rule, and `object-as` now also renames `id:` to `as:` on graph
  relationships, times, metrics and segments.
