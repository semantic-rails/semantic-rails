- `semantic_rails.embedding` is named as the engine's one public Python surface: the package
  docstring, the README and the embedding, MCP, query API and contracts docs now all say so, and
  their examples import from it. From 0.4.0 the facade follows Semantic Versioning (minor
  releases carry breaking changes while the version starts with 0); see "Stability" in
  [docs/EMBEDDING.md](docs/EMBEDDING.md). The promise isn't retroactive, and no compatibility
  shims are added for earlier changes.
