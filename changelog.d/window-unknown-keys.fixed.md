- A `rolling` or `conversion` window (and a predicate's or anchored scoped aggregate's window)
  with a key the engine doesn't read, such as `exclude_current` or `include_end`, is refused
  and names the key. Before, the key was ignored and the default window's number came back as
  if the request had been understood.
