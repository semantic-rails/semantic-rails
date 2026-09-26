- `docs/EMBEDDING.md` describes how changes to `semantic_rails.embedding` are staged:
  the new form ships next to the old one, the old one is deprecated with a named removal
  release, and it is removed only after embedders have moved. The test suite now checks
  every facade name, attribute and call shape a known embedder uses.
