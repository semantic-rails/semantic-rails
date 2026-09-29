- Two relationships between the same pair of entities (for example a leg's origin and
  destination airport) are now both kept. Previously the loader kept only one, and a query
  that reached the airport could silently return origin or destination values depending on
  declaration order. Every such query is now refused with `AMBIGUOUS_PATH` naming the routes
  and how to pin one: the airport's city, its key column, a filter on either, and a metric
  predicate on the airport, including when one role joins to a non-key column of the airport. A lower `path_preference` on the intended relationship pins it for
  every query; a `graph.path_preferences` row pins only queries from its source entity to its
  target entity. Parsing the package warns with `RELATIONSHIP_ROLES_UNPINNED` unless exactly
  one relationship has the lowest `path_preference`, whichever side each is declared from. A pinned
  role reads the airport's key through the pinned relationship's join, so a leg whose code matches
  no airport is not counted. When a `graph.path_preferences` row exists for the pair (even one
  that names a route through another entity), the key is read through path selection too, so a
  row never pairs one airport's city with another airport's code. Two authored `graph.relationships` entries on the same `via` columns
  are refused at load instead of one silently replacing the other. `path_preference: 0` now counts
  as the lowest preference (it was read as 100), and a negative value is refused. When several
  relationships join one pair, a rollup aggregation must be allowed by every one that lists any.
  `upsert_relationship` refuses a pair that may have several roles instead of rewriting one of them.
