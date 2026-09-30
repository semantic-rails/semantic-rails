- The query key `path_policy` (`preference`, `ask_if_ambiguous`) is removed from the query IR,
  its schemas and a segment's `membership:`. It never changed an answer: `preference` only entered
  a cache key, and `ask_if_ambiguous` was never read. A query or segment that still sends it is
  refused as an unknown key; delete it. A package's `graph.path_policy.max_hops` is unchanged.
