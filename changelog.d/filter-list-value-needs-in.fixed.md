- A filter comparison that takes one value (`=`, `!=`, `<`, `<=`, `>`, `>=`,
  `LIKE`, `NOT LIKE`) now rejects a list value with `INVALID_QUERY` and a
  `USE_IN_FOR_LIST_VALUE` hint to use `IN`. Before, the list was rendered as
  one string literal, such as `store_name = '[''Philadelphia'', ''Brooklyn'']'`:
  a `where` filter validated and silently returned no rows, and a
  `metric_filters` comparison failed in the warehouse.
