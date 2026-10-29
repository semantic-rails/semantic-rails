- The text words `contains`, `starts_with` and `ends_with` (and their `not_` forms) read an id as
  its text on every warehouse, whatever type it is stored as, so a product or order code is
  found by a fragment (`JAF-` in `JAF-003`), a UUID by its first characters and a numeric id by
  its digits (`ends_with` `"03"` matches 103 and 203). They used to refuse an id. `valid-values`
  `search` reads an id the same way, with the same paging and counts as text, over the values
  the caller's row filters leave. `is` and `not` still compare an id as it is stored.
