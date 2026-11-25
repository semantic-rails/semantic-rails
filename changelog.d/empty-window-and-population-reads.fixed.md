- On Snowflake, BigQuery, Databricks and Athena, a series over an open window (`start` or `end`
  alone, with or without `fill`) now reads `0` in a period where a sum or count has no rows
  while its measure has data outside the window and the period lies inside the measure's own
  loaded range, as DuckDB and Postgres already did; it read `NULL`. Past the measure's own last
  row it still reads `NULL`, now with `NO_DATA_YET`. "Revenue and refunds until mid-November"
  reads `0` refunds in a month with none when refunds were recorded later, and their average
  stays `NULL`: an average of nothing has no value. A window total there (no `grain`) and a
  part of a joined call, which can lie past the data's edge, still read `0` only while the
  measure has data inside the window, and so does every empty period on ClickHouse
  ([issue #201](https://github.com/semantic-rails/semantic-rails/issues/201)).
- A distinct-population count (a count of distinct customers) with no `group_by` and no `time`
  now reads `NULL` with `NO_DATA_IN_SCOPE` when its filters keep no row, in either observation
  scope; it read `0` with no warning. One that read rows keeps its count, `0` included, and one
  beside an entity filter counts the entities the filter keeps, `0` when it keeps none
  ([issue #203](https://github.com/semantic-rails/semantic-rails/issues/203)).
