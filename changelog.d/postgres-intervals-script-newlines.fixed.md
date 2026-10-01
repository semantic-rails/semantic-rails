- Postgres interval results now use the same exact duration values and JSON
  interval metadata as DuckDB, including its 30-day month convention. Values
  beyond Python's duration range or microsecond precision refuse explicitly.
- SQL seed and CSV post-load scripts reject bare carriage returns with an error
  naming the source file before executing any script statement. CRLF line
  endings preserve and execute every statement like LF line endings.
