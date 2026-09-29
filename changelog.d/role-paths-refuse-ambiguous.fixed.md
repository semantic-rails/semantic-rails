- Two relationships between the same pair of entities (for example a leg's origin and
  destination airport) are now both kept. Previously the loader kept only one, so a query for
  the airport's city silently returned origin or destination cities depending on declaration
  order. Such a query is now refused with `AMBIGUOUS_PATH` naming the routes and how to pin
  one (`graph.path_preferences`, or a lower `path_preference` on the intended relationship),
  and parsing the package warns with `RELATIONSHIP_ROLES_UNPINNED` until a route is pinned.
