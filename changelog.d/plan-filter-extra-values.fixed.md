- `plan` no longer reports `status: ok` for a filter that keeps or drops values the question
  doesn't name. "Revenue for Brooklyn" filtered with `IN ["Brooklyn", "Philadelphia"]` and
  no grouping by store, or "revenue excluding Brooklyn" filtered with
  `NOT IN ["Brooklyn", "Philadelphia"]`, now downgrades to `low_confidence` with a
  `filter_values_unrealized` gap. Grouping by the field still accepts the extra kept value,
  since each value gets its own row, and a filter that keeps exactly the named values, as in
  "revenue for Brooklyn and Philadelphia", is still `ok`.
