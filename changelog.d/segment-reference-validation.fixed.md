- Package validation (`parse-config`, `validate-config`, `check` and
  `semantic_rails.embedding.validate_runtime_package`) now rejects segments that
  `catalog`, `inspect` and the `segment-*` commands cannot serve: an `entity:`
  that names no entity, including inside membership `metric_predicate` filters
  (the error suggests the closest entity id), a basis metric or preview
  dimension that `catalog` rejects, or a derived query that does not compile.
  These packages used to pass validation and then fail those commands on every
  request.
