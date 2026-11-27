- Inline filter words use a column's declared unknown values in any spelling the warehouse reads as
  the same column. In the DuckDB example, `missing` on `"customer ref"` or `"select"` totals 15 where
  it totaled 5. On a joined reference, `missing: false` totals 20 where it totaled 30.
  A `contains` comparison with one matching dimension now totals 20 where it refused with
  "no dimension". `is "UNKNOWN"` and `not "UNKNOWN"` are refused as they are in `where`.
  With conflicting declarations, `missing`, `is` and `not` return `INVALID_QUERY` and name the
  dimensions. Reordered or repeated unknown values agree and yield one result.
  Integer and decimal literals such as `1` and `1.0` remain distinct because the warehouse
  may compare them differently.
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
  On Athena, a comparison written in a categorical dimension's spelling keeps its text literal,
  and a comparison using an undeclared spelling refuses a text literal if the matching
  dimensions disagree on temporal typing.
