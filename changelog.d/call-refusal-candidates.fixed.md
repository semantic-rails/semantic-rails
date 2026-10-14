- `order_by` accepts the id of a selected measure or metric, in a call, a plan seed or a package
  example, and orders by the one select item that reads it as itself. An id read so twice, or
  only with an `aggregation`, `temporal_role` or `parameters` override, is refused with
  `INVALID_ORDER_BY` and those items' columns in `closest_matches`.
- A raw `column` expression is still refused, now at its select path with `supported_kinds` (the
  kinds a select item can be) and a hint listing them, and the dimensions defined on that column
  (on its entity or table, when named) in `closest_matches`.
- A `routes` `via` hop that names no relationship is refused with the relationship ids that join
  `from` to `to` in one hop (`closest_matches`), else up to three of the pair's routes ready to
  send (`other_routes`), else the relationships touching either entity, and a hint that `via`
  takes relationship ids.
