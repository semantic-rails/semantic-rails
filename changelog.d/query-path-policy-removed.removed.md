- **Breaking:** the query key `path_policy` (`preference`, `ask_if_ambiguous`) is removed from
  Query IR v1 (`schemas/query_ir.v1.json`), the preview v2 schema, and so from the HTTP API, the
  query MCP and a segment's `membership:`. Query IR stays at v1: before 1.0 the project follows
  [Semantic Versioning](https://semver.org/spec/v2.0.0.html)'s major-zero rule, under which a
  0.x release may change the public API. The key never changed an answer: `preference` only
  entered a cache key, and `ask_if_ambiguous` was never read. A query that still sends it is
  refused with `INVALID_QUERY` (`details.unsupported_keys: ["path_policy"]`), and `check` and
  `validate` report it in a segment's `membership:` like any unknown key; delete it. A package's
  `graph.path_policy.max_hops` is unchanged.
- A relationship's `path_preference` weight is removed: a number on a relationship never says
  which route a question means. A package that still sets it fails to load with `INVALID_CONFIG`,
  naming the relationship; delete it, and record the route for each entity pair that needs one as
  a `graph.path_preferences` row. Relationship metadata no longer lists it, and
  `RELATIONSHIP_ROLES_UNPINNED` now warns for every pair of entities joined on different columns.
