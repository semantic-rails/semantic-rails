- Package validation (`parse-config`, `validate-config`, `check` and
  `semantic_rails.embedding.validate_runtime_package`) now rejects segments that
  `catalog`, `inspect` and the `segment-*` commands cannot serve: an `entity:`
  that names no entity, including inside membership `metric_predicate` filters
  (the error suggests the closest entity id when one is close), a segment that
  `catalog` rejects, such as one with a preview dimension from another entity,
  or a derived query that does not compile. These packages used to pass
  validation, and then `catalog` and `inspect`, or the `segment-*` commands,
  failed on every request.
