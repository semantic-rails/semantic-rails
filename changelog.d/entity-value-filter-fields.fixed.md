- `entity_value.where` items that name a field are now refused instead of being
  applied to the per-entity value. Put dimension filters in the query's top-level
  `where`; other unsupported item keys are also refused with `INVALID_QUERY`.
