- `plan` drafts the governed metric, not the measure it filters, when the question names that
  metric in any word order: "how many stores were active last week" answers with Active
  stores, not the all-kinds count it narrows. A draft that still reads such a measure is
  `low_confidence` with a `governed_metric_unrealized` gap naming the metric, so it is never
  ready to execute without a warning.
- A measure authored with `publish: false` that a metric reads through a filter, and that no
  metric aggregates whole, is a building block: `discover` no longer lists it and `plan`
  answers with its metric. It stays queryable by id. See
  [docs/PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md#building-block-measures).
