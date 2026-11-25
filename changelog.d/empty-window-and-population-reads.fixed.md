- On Snowflake, BigQuery, Databricks and Athena, a series over an open window (`start` or `end`
  alone, with or without `fill`) now reads `0` in a period where a sum or count has no rows
  while its measure has data outside the window, as DuckDB and Postgres already did; it read
  `NULL`. "Revenue and refunds until mid-November" reads `0` refunds in a month with none when
  refunds were recorded later, and their average stays `NULL`: an average of nothing has no
  value. A window total there (no `grain`), which can lie past the data's edge, still reads `0`
  only while the measure has data inside the window, and so does every empty period on
  ClickHouse ([issue #201](https://github.com/semantic-rails/semantic-rails/issues/201)).
- A distinct-population count (a count of distinct customers) with no `group_by` and no `time`
  now reads `NULL` with `NO_DATA_IN_SCOPE` when its filters keep no row, as a count does; it
  read `0` with no warning. One that read rows keeps its count, `0` included, and one beside an
  entity filter counts the entities the filter keeps, `0` when it keeps none
  ([issue #203](https://github.com/semantic-rails/semantic-rails/issues/203)).
