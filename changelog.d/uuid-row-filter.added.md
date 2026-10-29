- A `row_filter` on a UUID column (a tenant or owner key) takes `type: uuid`, and filters
  rows on Postgres, where a row filter on a `uuid` column refused every call before. The
  trusted attribute may hold the UUID in any case, with or without hyphens or braces, and
  binds as its lower-case text; a value that isn't a UUID is denied before any SQL runs. See
  [docs/PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md).
