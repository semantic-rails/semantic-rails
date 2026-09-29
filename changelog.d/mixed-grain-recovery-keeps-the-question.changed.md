- `MIXED_GRAIN_INVALID` no longer offers a `closest_valid_query` that swaps the requested
  measure or dimension for another one: a different measure answers a different question
  (item revenue is not order revenue). `replace_measure` and `replace_dimension` still name
  the compatible objects, and `details.closest_compatible_measure_query` and
  `details.closest_compatible_dimension_query` are gone. A `use_time_grain` fix keeps its
  query, since it asks the same question.
