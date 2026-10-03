- Metric constraints now also apply to filters inside inline expressions. A filter
  written in an aggregate's `filter`, a `semi_additive` expression's `filter` or a
  scoped aggregate's `where` (including inside a window or ratio) that counts for a
  governed measure must use a field in
  `allowed_where`; otherwise the query is refused with `POLICY_DENIED` and a
  `disallowed_where` violation carrying `"source": "inline_expression"`. Such a filter
  nested under a predicate, `metric_filters` or a conversion operand counts for
  every governed object in the query. Inline filters never satisfy `required_where`,
  and always count as metric filters for
  `allow_metric_filters: false`. A package-wide constraint with `allowed_where`
  refuses `aggregate_if`, whose condition reads columns rather than fields.
