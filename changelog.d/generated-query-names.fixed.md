- Queries now read package tables whose names match generated query names such as
  `leaf_1`, including uppercase and schema-qualified spellings.
- Relation pipelines keep their filters when a declared output matches a
  generated stage name. Stored inputs keep their identity when a generated
  stage or a differently cased pipeline output has a matching name.
- Subtotals backed by relation pipelines report loading warnings and the last
  loaded date. Observation warnings can read dependencies from nested queries.
