- A `graph.path_preferences` row no longer covers only queries that start at its
  `source_entity` and end at its `target_entity`: it holds wherever a route walks its pair.
- A row recorded for the reverse pair no longer stops the direct read of a start entity's one
  own key; the key and the target's other columns still come from the same route.
