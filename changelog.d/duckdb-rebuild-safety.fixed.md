- The DuckDB runtime no longer rebuilds a database its package's seed did not
  create. The existence check resolves the relations a package reads
  (schema-qualified tables, views, relation-pipeline sources) the way compiled
  SQL does, and a database that lacks one is rebuilt only when this package's
  seed built it and nothing has changed it since. Otherwise the runtime raises
  `INVALID_CONFIG` with `details.missing_relations` and leaves the file alone;
  `SEMANTIC_RAILS_ALLOW_DB_RESEED=1` allows the replacement as an explicit
  opt-in. A file another process is writing is never replaced, and databases
  holding macros or user-defined types, or on Windows, are reported rather than
  rebuilt automatically. On a filesystem without hard links the runtime does not
  build the database at all without the opt-in. Databases built by earlier
  releases record no seed provenance: delete one once to rebuild it.
