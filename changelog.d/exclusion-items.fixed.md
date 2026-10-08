- `plan` holds an exclusion unless the draft drops every value it names, each with its own
  `IS DISTINCT FROM` filter that keeps rows with no recorded value, and excludes nothing the
  question doesn't name. Dropping some of the listed values, an unrelated negative filter, `!=`
  or `NOT IN`, a listed word the catalog can't match, or a list joined by ";", "/", "plus",
  "as well as", "along with", "alongside", "together with", a dash, a line break or brackets
  no longer reaches `ready_for: ["execute"]`. "other than", "apart from", "aside from", "minus"
  and "outside of" now exclude too.
- A time exclusion ("signups not in June 2024", "excluding Jun. 25, 2024") holds instead of
  being read as the question's window, since Query IR has no window complement.
- For "excluding web", `plan` now drafts `IS DISTINCT FROM 'web'` instead of `= 'web'`, so
  such questions can be ready to execute. The Query IR contract lists `IS DISTINCT FROM` as a
  `where` operator; it was already accepted.
