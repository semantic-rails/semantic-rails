- Embedding hosts can attach typed, immutable `TrustedAttributes` (for example a customer ID from a
  verified token) to a `RequestContext`, imported from `semantic_rails.embedding`. The engine
  carries them through every transport and internal call; request bodies, headers and plans
  can't set or replace them, and they never appear in the public `request_context`, echoed
  queries, errors or audit events. `row_filter` policies read them. See
  [docs/EMBEDDING.md](docs/EMBEDDING.md).
