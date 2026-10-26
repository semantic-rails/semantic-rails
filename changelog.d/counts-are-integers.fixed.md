- Counts come back as integers, whatever numeric type the warehouse widens them to. Postgres sums
  bigint counts as `NUMERIC` (a count read from an aggregate table, a running total or a
  period-to-date count), and Snowflake ADBC's high-precision results type every `NUMBER` count as
  a decimal; both came back as decimal text (`"6"`) with `column_types` `decimal`. A count, a row
  count (`expr: 1`) and a running total of either now read `6` with type `integer`. Sums of stored
  columns, including a stored count column, ratios and averages keep the driver's type. A count
  beyond 2^53 is an exact JSON integer, which a client reading numbers as doubles rounds.
