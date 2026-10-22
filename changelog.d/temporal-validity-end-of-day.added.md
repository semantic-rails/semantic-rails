- A relationship's `temporal_validity` takes `as_of: end_of_day`: a hop into the history reads
  the version in force at the end of each row's day on the query's clock, instead of at the
  row's own time. Use it for a history written a moment after the row it describes, such as a
  plan written seconds after its workspace: "new workspaces by the plan they signed up on" then
  reads each workspace's plan instead of an empty Plan for every row. `as_of: time` (the
  default) keeps today's answers. Route readings and `NULL_PRESERVING_HISTORY` say "valid at
  the end of the day", and `temporal_validity` refuses unknown keys and an `as_of` without
  `valid_from`.
