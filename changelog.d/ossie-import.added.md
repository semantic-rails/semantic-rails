- `semantic-rails import --from ossie` reads a document written by `export --format ossie` back
  into a package. With the sidecar the package comes back exactly, checked by exporting it again,
  and a document edited since the export is refused. Without the sidecar, datasets, column fields,
  joins to a primary key and metrics in the export's aggregate SQL are imported, with the types
  and aggregations it defaults counted in warnings, and anything else is skipped with a warning.
  A refused import leaves no files behind. Reading other Ossie 0.1.x and 0.2 documents is
  experimental. See [docs/OSSIE.md](docs/OSSIE.md).
