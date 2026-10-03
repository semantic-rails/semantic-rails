- Resolve relative time ranges using the temporal role's local date, so equivalent
  UTC and offset timestamps produce the same bounds. Refuse coarse relative periods
  on non-default calendars; use exact dates for those periods.
- Apply the same whole-day bounds to DATE clocks and calendar fill, including an
  end day when the exclusive end falls after midnight, after timezone conversion
  and when a snapshot or population clock differs from the query's time axis.
- Convert DATE clocks from midnight in their declared storage zone on DuckDB,
  MotherDuck, DuckLake and Postgres, preserving both local days across timezone
  boundaries whatever the session zone. Other warehouses keep their existing
  conversion SQL.
- Apply entity-only predicate windows using valid logical field filters on
  unconverted roles. On roles requiring timezone conversion, refuse entity-only
  predicate windows, contextual metric predicates joined on its time period, and
  conversion metrics queried on it, rather than comparing local bounds or buckets
  against raw stored values.
