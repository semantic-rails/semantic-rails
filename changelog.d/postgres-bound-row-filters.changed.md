- Run `postgres_native` through ADBC with bound access-policy row filters, exact
  Decimal and aware timestamp results, bounded fetching and millisecond deadlines.
  The `postgres` and `all` extras now install ADBC and PyArrow instead of psycopg;
  `schema` selects one exact, case-sensitive schema name. Queries preserve
  inherited statement timeouts and restore prior session settings after overrides.
  Plain SQL accepts JSON operators; parameterized SQL still refuses `?` operators.
  Session zones unavailable to Python return aware UTC timestamps.
- Split seed scripts around SQL comments without treating comment apostrophes
  or semicolons as literal or statement boundaries.
