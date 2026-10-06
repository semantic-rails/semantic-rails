- `plan` drafts the governed metric, not the measure it filters, when the question names that
  metric in any word order: "how many stores were active last week" answers with Active
  stores, not the all-kinds count it narrows. A draft that still reads such a measure is
  `low_confidence` with a `governed_metric_unrealized` gap checking the whole question,
  including "stores that were active last week". Only visible governed metrics are selected
  or named in responses. A building block without a visible metric is held with a generic
  message. The swap requires the draft's time role to equal the metric's own `temporal_role`.
  A published measure stays a legitimate answer when the question doesn't name a governing
  metric, as in "stores last week".
- A measure authored with `publish: false` that a metric reads through a filter, and that no
  metric aggregates whole, is a building block under `schema_strict`: `discover` no longer
  lists it and `plan` answers with its metric. It stays queryable by id. See
  [docs/PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md#building-block-measures).
