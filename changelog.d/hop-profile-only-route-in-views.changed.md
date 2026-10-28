- A caller whose view keeps one route between two entities now sees `route_basis: only_route` in
  the answer's `hop_profile`, whatever chose that route among routes the caller can't see: a
  recorded row, the start's own key or a lookup default. It used to name the rule, which showed
  that other routes exist. A history declared `many_to_many` now reads as "any of" its rows in
  route meanings: its versions valid at one time can overlap, so no one version is read. One rule
  (a validity window holds versions of one row only when its relationship has a one side) now
  decides route defaults, route wording and a hop's safety at the query's time.
