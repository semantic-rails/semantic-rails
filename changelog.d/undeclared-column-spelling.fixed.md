- Inline filter words use a column's declared unknown values in any spelling the warehouse
  reads as the same column. If dimensions on that column disagree on which values are unknown,
  the comparison returns `INVALID_QUERY` and names the dimensions.
