- Two relationships between the same pair of entities (for example a leg's origin and
  destination airport) are now both kept. Previously the loader kept only one, and a query
  that reached the airport could silently return origin or destination values depending on
  declaration order. Every such query is now refused with `AMBIGUOUS_PATH` naming the routes
  and how to pin one: the airport's city, its key column, a filter on either, and a metric
  predicate on the airport. A lower `path_preference` on the intended relationship pins it for
  every query; a `graph.path_preferences` row pins only queries from its source entity to its
  target entity. Parsing the package warns with `RELATIONSHIP_ROLES_UNPINNED` unless exactly
  one relationship has the lowest `path_preference`. `upsert_relationship` refuses a pair that
  already has several relationships instead of rewriting one of them.
