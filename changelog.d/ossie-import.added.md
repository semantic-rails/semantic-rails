- `semantic-rails import --from ossie` reads an Apache Ossie 0.1.x or 0.2 document into a package.
  With the sidecar that `export --format ossie` writes, the package comes back exactly, and the
  report says whether exporting it again gives the same files. Without the sidecar, datasets,
  column fields, joins and metrics in the export's aggregate SQL are imported with counted
  defaults, and anything else is skipped with a warning. See [docs/OSSIE.md](docs/OSSIE.md).
