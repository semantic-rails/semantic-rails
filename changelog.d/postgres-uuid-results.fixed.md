- On Postgres, a native `uuid` column can now be grouped by, listed and searched. Its values read
  back as their lower-case hyphenated text, with `column_types` `{"type": "uuid"}`, as on DuckDB.
  Before, any call that returned a uuid column (a `group_by`, a `valid-values` listing or
  `search`) was refused with `RESULT_TYPE_UNSUPPORTED`. Filters and counts on it already worked.
- QUERY_CALL now says how an id stored as a float reads as text: `203.0` on DuckDB, Databricks
  and Athena, and `203` elsewhere. Store ids as integers or text.
