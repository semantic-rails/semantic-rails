- Counts come back as integers on every warehouse. Postgres sums bigint counts as `NUMERIC`
  (a count read from an aggregate table, a running total or a period-to-date count), and
  Snowflake ADBC's high-precision results type every `NUMBER(18,0)` count as a decimal; both
  came back as decimal text (`"6"`) with `column_types` `decimal`. A count now reads `6` with
  type `integer`; sums, ratios and averages keep the driver's type.
