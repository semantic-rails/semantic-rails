- Inline filter words use a column's declared unknown values in any spelling the warehouse reads as
  the same column. In the DuckDB example, `missing` on `"customer ref"` or `"select"` totals 15 where
  it totaled 5. On a joined reference, `missing: false` totals 20 where it totaled 30.
  In the single-sentinel `Customer Ref` example, an alternate spelling reads the sentinel row
  as null, so `gt 'R1'` totals 8 where it totaled 10 and `NOT (is 'R1')` totals 24 where it
  totaled 26.
  A `contains` comparison with one matching dimension now totals 20 where it refused with
  "no dimension". `is "UNKNOWN"` and `not "UNKNOWN"` are refused as they are in `where`.
  With conflicting declarations, `missing`, `is` and `not` return `INVALID_QUERY` and name the
  dimensions. Reordered or repeated unknown values agree. With equivalent lists, the
  alternate spelling's `missing` total is 10 where it was 5.
  The declaration agreement check distinguishes integer and decimal literals such as `1`
  and `1.0` because the warehouse may compare them differently. `is 1` and `is 1.0` are both
  refused against `unknown_values: [1.0]`, as in `where`.
- A declared measure whose expression compares a column with a filter word or a text word
  now depends on the dimensions over that column, as a metric's filter does. A `deny` policy
  on such a dimension refuses the measure with `POLICY_DENIED`. A `hidden` policy hides the
  measure and the metrics over it, including when a rollup would answer. Previously the
  measure answered and could ignore unknown values for the restricted audience, returning 1
  where other callers got 3 in the regression example.
- A filter word now respects `filterable: false` on every dimension over the column and every
  spelling the warehouse reads as that column. For a caller with a hidden object, a raw-column
  aggregate refusal omits the dimension name.
- A measure whose comparison newly inherits unknown values reads from the base relation. The
  regression example now returns 10, including when a rollup stored the previous total of 5.
  To make the rollup eligible again, first rebuild it with the current unknown-value handling,
  then write the column as its dimension declares it.
- On Athena, an inline filter word written in a categorical dimension's spelling keeps its text
  literal unless that spelling also has a date or timestamp declaration. When every matching
  dimension declares a date or timestamp, an undeclared spelling now uses a `TIMESTAMP` literal
  for ISO date text. On any warehouse that reads the spellings as one column, an inline filter
  word comparing text on an undeclared spelling returns `INVALID_QUERY` if the matching dimensions
  disagree on temporal typing. The DuckDB example with different declared spellings previously
  returned 25. Filter on the dimension by ID to choose a declaration.
