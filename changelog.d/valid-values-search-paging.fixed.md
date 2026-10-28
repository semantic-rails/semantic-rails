- `valid-values` with `allow_live_query` searches, pages and counts every value of the
  dimension. It used to read only the first `max(limit + offset, 100)` values, so a `search`
  for a value sorting past them answered `total_count: 0`, and a longer list ended with
  `has_more: false` as if complete. `search` now runs in the warehouse as the `contains` filter
  word's match (literal, ignoring case; `_` and `%` are text), over the values the caller's
  row filters leave, and `offset` and `limit` page there. `has_more` is exact, and
  `total_count` is the number of matching values once a page holds the last of them, or `null`
  before that; it is also `null` on a `needs_live_query` answer, which reads no values.
- A declared value domain matches `search` the same way: literal, ignoring case, in a value, its
  label or an alias. A search made only of punctuation, such as `_`, used to match every value.
- A live `search` on a dimension that isn't text (a number, date or id) is refused with
  `INVALID_QUERY` at `search` before any read: list its values without `search`.
