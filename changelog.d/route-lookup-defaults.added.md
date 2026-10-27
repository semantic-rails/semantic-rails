- A question whose two entities connect by several routes answers when exactly one route is a
  lookup (one row at every hop: a to-one hop, or the version valid at the time), instead of
  being refused with `AMBIGUOUS_PATH`: a section's instructor, not any of its enrolled students.
  With several lookups, each entity's own keys decide between them (a seat's building is its
  room's building). A route reading "any of" rows is never chosen this way, and no question
  that answered before changes its answer. Each such answer says which reading it took and
  which routes it was chosen over (`routes[*].basis: default`, `by`, `meaning`,
  `other_routes`, at every verbosity); `route_census.defaults` lists every pair it answers.
  Opening reverse hops on a forward-only package now keeps every answer it gave: on a
  synthetic 74-entity campus warehouse, all 291 forward-only answers stay identical when it is
  opened both ways, where before 104 of its 253 answers were refused.
