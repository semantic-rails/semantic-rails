- A dimension id where a call reads a measure or metric is refused at its path as a dimension,
  naming the measures that count its column's distinct values (`closest_matches`) and
  `group_by` to split by it, instead of `Unknown measure` offering the dimension itself.
- `PATH_NOT_FOUND` names a start the query reads (a query without a measure no longer names an
  entity it never used), says how that start was chosen (`details.anchor`), lists the query's
  items it can't reach (`details.unreachable_items`) and offers to drop them or split the
  query. A start that reaches nothing says what blocked it (`details.blocked_by`), the entities
  related to both ends and their shared key columns; a row-grain target gets no hint to author
  a relationship into it.
- `RESULT_TOO_LARGE` names the largest field (`details.largest_field`) and a concrete retry
  (`details.retry`: `row_format: "columns"`, or `sql_profile: "off"` when the SQL is returned).
- `FILTER_VALUE_NOT_FOUND` names at most 5 values and lists at most 10 (`details.missing_count`
  counts them all), and suggests only the same text in another case or a close spelling; an id
  dimension's value, a value with a digit or a weak match gets none.
- A date or timestamp filter value that isn't an ISO date or date-time is refused at
  `where[i].value` instead of failing in the warehouse; every filter value type refusal names
  its path.
- An unknown arithmetic `op` in a package expression is refused when the package loads, at
  `expression.op` with `add`, `subtract`, `multiply` and `divide` and the closest of them.
- An engine refusal at `select[i]` names the caller's index when a dimension string earlier in
  `select` moved to `group_by`.
- A plan held for an unresolved "current" or "now" names the package's current-state flags and
  snapshot measures that share a word with the question (`use_current_state`).
- The `between` expression shape example compares a measure, so it validates as a select item.
