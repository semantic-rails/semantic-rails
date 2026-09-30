- Run `postgres_native` through ADBC with bound access-policy row filters, exact
  Decimal and aware timestamp results, bounded fetching and millisecond deadlines.
  The `postgres` and `all` extras now install ADBC and PyArrow instead of psycopg;
  existing connection options continue to work.
