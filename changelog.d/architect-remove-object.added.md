- The Architect MCP's `remove_object` removes a model, dimension, time,
  measure, metric, segment, relationship, example or test in one transaction,
  archiving its YAML. It refuses a removal that would break a measure, metric
  or segment, and previews the examples and tests it breaks, the files that
  still mention it, and the behaviour impact. `upsert_model` and
  `upsert_metric` take `replace: true` to rewrite an object instead of merging
  into it, and `upsert_model` takes a `label`.
