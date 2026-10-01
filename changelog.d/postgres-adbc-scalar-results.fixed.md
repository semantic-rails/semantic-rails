- Return TIME, BYTEA and UUID results through the Postgres ADBC adapter with
  exact values and their logical result types, including UUID key dimensions.
- Refuse Postgres TIME values outside Python's exact clock range, including
  `24:00:00`, instead of silently wrapping them to midnight.
