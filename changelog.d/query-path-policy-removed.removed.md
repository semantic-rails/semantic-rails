- The query key `path_policy` (`preference`, `ask_if_ambiguous`) is removed from the query IR,
  its schemas and a segment's `membership:`. It never changed an answer: `preference` only entered
  a cache key, and `ask_if_ambiguous` was never read. A query that still sends it is refused as an
  unknown key, and `check` and `validate` report it in a segment's `membership:` like any unknown
  key; delete it. A package's `graph.path_policy.max_hops` is unchanged.
- A relationship's `path_preference` weight is removed: a number on a relationship never says
  which route a question means. A package that still sets it fails to load with `INVALID_CONFIG`,
  naming the relationship; delete it, and record the route for each entity pair that needs one as
  a `graph.path_preferences` row. Relationship metadata no longer lists it, and
  `RELATIONSHIP_ROLES_UNPINNED` now warns for every pair of entities joined on different columns.
