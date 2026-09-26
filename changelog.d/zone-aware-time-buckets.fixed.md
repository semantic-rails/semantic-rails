- A `TIMESTAMP WITH TIME ZONE` time column on DuckDB, MotherDuck, DuckLake and Postgres now
  buckets and filters in its time role's `timezone` (UTC by default) at every grain. Before,
  day and week answers, and month answers from the base table, followed the server's or
  machine's session time zone, so they could disagree with each other and with a rollup built
  in UTC. The engine sets the session zone for each query only, and never leaves it changed on
  its own or a host's connection. Naive `TIMESTAMP` and `DATE` columns are unaffected. Other
  warehouses are unchanged; see "`times:` — temporal roles" in
  [docs/PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md).
