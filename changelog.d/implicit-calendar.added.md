- `rolling`, `prior_period` and `time.fill` now work in a package that declares no calendar:
  a query on the default calendar fills its periods from an implicit Gregorian calendar the
  engine generates in SQL (calendar months, quarters and years, Monday weeks, in the time
  role's zone), spanning the query's window or the data's first to last period. They used to
  fail with "time.fill requires a calendar entity in the package". An authored calendar still
  fills when the package has one; any other `calendar_id`, such as a fiscal calendar, still
  needs its calendar authored and is refused without it. Not available on ClickHouse. See
  [docs/QUERY_IR_SCHEMA.md](docs/QUERY_IR_SCHEMA.md).
