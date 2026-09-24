- DuckDB runtime bootstrap now creates a missing seed database with atomic,
  no-clobber publication and never replaces an existing database. Validation
  reports missing schema-qualified tables, views, and relation-pipeline sources
  as `INVALID_CONFIG` with `details.missing_relations`, regardless of seed
  provenance or the legacy `SEMANTIC_RAILS_ALLOW_DB_RESEED` setting. Existing
  databases are catalog-probed in a separate process so a stale in-process
  catalog cannot certify a replaced file and the probe cannot release a
  serving connection's process-wide lock. Operators must build missing
  relations with the database owner or explicitly remove a backed-up,
  disposable seed database before restarting.
