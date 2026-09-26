- `docs/EMBEDDING.md` describes how changes to `semantic_rails.embedding` are staged:
  the new form ships next to the old one, the old one is deprecated with a named removal
  release, and it is removed only after embedders have moved. The test suite now checks
  the facade names, attributes, call shapes and implemented protocols recorded from a
  known embedder's code.
