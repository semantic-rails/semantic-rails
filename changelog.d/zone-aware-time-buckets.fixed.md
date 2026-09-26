- A `TIMESTAMP WITH TIME ZONE` time column on DuckDB, MotherDuck, DuckLake and Postgres now
  buckets and filters in its time role's `timezone` (UTC by default) at every grain. Before,
  day and week answers, and month answers from the base table, followed the server's or
  machine's session time zone, so they could disagree with each other and with a rollup built
  in UTC. Each query now runs with the session time zone set to its time role's zone (UTC
  without one), only for that query, on the engine's connection or a host's. Everything
  zone-dependent in the query follows that zone: authored `call` expressions over zone-aware
  values, `now()` and `current_date`, and the offset shown on zone-aware values in rows. A query
  whose measures use time roles in other zones returns a `TIME_ZONE_NOT_APPLIED` warning. Naive
  `TIMESTAMP` and `DATE` columns are unaffected, and other warehouses are unchanged; see
  "`times:` — temporal roles" in [docs/PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md).
  On every warehouse, `certify_aggregate_relation` no longer certifies a rollup under a role
  whose `timezone` isn't UTC (`timezone_not_utc`), so those queries use the base tables. On
  DuckDB, MotherDuck, DuckLake and Postgres, a host builds a rollup it certifies, and runs the
  paired queries, with the session time zone set to UTC.
