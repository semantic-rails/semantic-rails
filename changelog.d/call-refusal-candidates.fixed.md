- `order_by` accepts the id of a selected measure or metric and orders by the one select item
  that reads it; an id selected more than once is refused with `INVALID_ORDER_BY` and those
  items' columns in `closest_matches`.
- A raw `column` expression is still refused, now with `supported_kinds` and a hint listing them,
  and the dimensions defined on that column (on its entity or table, when named) in
  `closest_matches`.
- A `routes` `via` hop that names no relationship is refused with the relationship ids that join
  `from` to `to` in one hop (else those touching either entity) in `closest_matches`, and a hint
  that `via` takes relationship ids.
