- The `RELATIONSHIP_ROLES_UNPINNED` warning is gone. Each pair of entities it reported is in the
  route census with the routes its queries are refused on, as is one relationship declared from
  both sides on the same columns, which the warning missed; `ROUTES_UNDECIDED` is the one warning
  for them all.
