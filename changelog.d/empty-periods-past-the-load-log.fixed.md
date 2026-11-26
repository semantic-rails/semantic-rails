- On Snowflake, BigQuery, Databricks and Athena, a series over a model whose `zero_through`
  names a load log (or `now`) now reads `NULL` with `NO_DATA_YET`, not `0`, in a period the
  load log doesn't cover yet, as DuckDB and Postgres already did. Such a period read `0`
  whenever another measure in the call had rows in it: "tickets opened and all tickets, by
  week" read `0` tickets opened for a week after the last load because a late ticket from that
  week was already in the table. 0.3.6 did this when the measure had rows elsewhere in the
  answer, and 0.3.7 also when it had rows only before the window. This holds with `start` or
  `end` alone and with no window. Periods the load log covers read as before.
