- Postgres refuses unsupported result column types with `RESULT_TYPE_UNSUPPORTED`
  instead of returning nested NUMERIC or JSON/JSONB values as strings. Supported
  scalar results retain their exact types, including NUMERIC as `Decimal`.
