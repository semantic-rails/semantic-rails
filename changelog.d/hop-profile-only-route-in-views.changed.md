- A caller whose view keeps one route between two entities now sees `only_route` wherever an
  answer states how that route was chosen: the `hop_profile`'s `route_basis`, a
  `ROUTE_PASS_THROUGH` warning's `details.route_basis`, and the `replaced` of the caller's own
  `routes` row. That holds whatever chose the route among routes the caller can't see: a
  recorded row, the start's own key or a lookup default. These fields used to name the rule,
  which showed that other routes exist. A caller with row filters and nothing hidden also reads
  `only_route` where an unrestricted caller reads `colocated_key` for a single direct key.
- A history declared `many_to_many` now reads as "any of" its rows in route meanings: its
  versions valid at one time can overlap, so no one version is read. One rule (a validity window
  holds versions of one row only when its relationship has a one side) now decides route
  defaults, route wording and a hop's safety at the query's time. Such a history with an
  explicit `safety: safe` no longer carries `NULL_PRESERVING_HISTORY`, the warning for one
  version valid at each row's time.
