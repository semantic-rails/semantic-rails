- A DuckDB database built from a package's seed no longer goes stale silently. When the seed
  files change after the build, query results and `project validate --mode runtime` include
  a `STALE_SEED_DATABASE` warning with the command that deletes the file; the next run
  rebuilds it from the current seed. Databases built before this release have no recorded
  seed hash; delete one once to start tracking it.
