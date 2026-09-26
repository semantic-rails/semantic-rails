- `semantic-rails import --from ossie` reads a document written by `export --format ossie` back
  into a package. With the sidecar the package comes back exactly, checked by exporting it again,
  and a document edited since the export is refused. Without the sidecar, datasets, column fields,
  joins and metrics in the export's aggregate SQL are imported with counted defaults, and anything
  else is skipped with a warning. Reading other Ossie 0.1.x and 0.2 documents is experimental. See
  [docs/OSSIE.md](docs/OSSIE.md).
