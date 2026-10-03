- Metric constraints now also apply to filters inside inline expressions. A filter
  written in an aggregate's `filter`, a `semi_additive` expression's `filter` or a
  scoped aggregate's `where` (nested, inside a window or ratio, in `metric_filters` or
  in a predicate's input) that cuts a governed measure must use a field in
  `allowed_where`; otherwise the query is refused with `POLICY_DENIED` and a
  `disallowed_where` violation carrying `"source": "inline_expression"`. Such a filter
  never satisfies `required_where`, and always counts as a metric filter for
  `allow_metric_filters: false`. A package-wide constraint with `allowed_where`
  refuses `aggregate_if`, whose condition reads columns rather than fields.
