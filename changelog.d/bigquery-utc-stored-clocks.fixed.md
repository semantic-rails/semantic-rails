- BigQuery: a clock stored in UTC (`column_timezone: UTC`) converts to its own zone as
  `DATETIME(CAST(column AS TIMESTAMP), zone)`, which BigQuery accepts for a `TIMESTAMP`,
  `DATETIME` or `DATE` column. The earlier `TIMESTAMP(column, 'UTC')` has no `TIMESTAMP` form,
  so an instant column declared as the refusal's hint asks was rejected.
