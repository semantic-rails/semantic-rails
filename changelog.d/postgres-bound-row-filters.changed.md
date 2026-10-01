- Run `postgres_native` through ADBC with bound access-policy row filters, exact
  Decimal and aware timestamp results, bounded fetching and millisecond deadlines.
  The `postgres` and `all` extras now install ADBC and PyArrow instead of psycopg;
  `schema` selects one exact, case-sensitive schema name. Queries preserve
  inherited statement timeouts and restore prior session settings after overrides.
  Plain SQL accepts JSON operators; parameterized SQL still refuses `?` operators.
  Bind scanning preserves identifiers containing `$` and E-string escapes.
  Session zones unavailable to Python return aware UTC timestamps.
- Split seed scripts only at unquoted semicolons, preserving statement text,
  comments and E-string escapes. Tagged and untagged dollar-quoted values retain
  comment delimiters and semicolons verbatim; unterminated quotes or block
  comments refuse the script before any statement executes.
