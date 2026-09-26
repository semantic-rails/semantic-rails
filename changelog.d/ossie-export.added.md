- `semantic-rails export --format ossie --output DIR` writes a package as an Apache Ossie 0.1.1
  document (`<package-id>.ossie.yaml`) that passes the spec's validator, plus a sidecar
  (`<package-id>.semantic_rails.json`) holding what Ossie 0.1.1 can't express. Each such construct
  gets a counted warning. Metrics, measures and relationships Ossie can't state faithfully are left
  out of the document instead of approximated, and semantic policies carry an extra warning that
  Ossie consumers won't enforce them. See [docs/OSSIE.md](docs/OSSIE.md).
