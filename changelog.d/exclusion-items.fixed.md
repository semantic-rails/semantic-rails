- `plan` holds an exclusion unless the draft drops every value it names, each with its own
  `IS DISTINCT FROM` filter that keeps rows with no recorded value, and carries no other
  predicate. Dropping some of the listed values, an unrelated negative filter, `!=`
  or `NOT IN`, a listed word the catalog can't match, or a list joined by ";", "/", "plus",
  "as well as", "along with", "alongside", "together with", a dash, a line break or brackets
  no longer reaches `ready_for: ["execute"]`. "other than", "apart from", "aside from", "minus"
  and "outside of" now exclude too.
- An exclusion also holds when its list has a character no item, separator or lead reads (a
  single-quoted name such as `'store'`, or a name with no letter or digit such as `-`), when an
  exclusion word or "including" falls inside a quoted string or a declared value name
  ("Including Top"), or when an exclusion word falls inside a grouping phrase ("revenue by
  store excluding Brooklyn", which would otherwise lose its grouping).
- Beside an exclusion the draft carries only one plain reference to a measure or metric the
  question names, the exclusion's own `IS DISTINCT FROM` filters, a `time` block with the
  subject's own clock, a grain and the window the question states, `group_by`, `order_by`
  and inert context; anything else holds, whether `plan` drafted it or the caller's
  `partial_query` supplied it: another filter on any field, a child group, a CASE or any
  other selected expression, a subject the question doesn't name, a metric filter, `limit`
  (even 0), `limits`, `route_decisions`, `temporal_role_overrides`, `observation_scope`,
  `export`, an unstated time bound or any key not listed. A question that both excludes and
  keeps values ("web signups excluding Top", "excluding Brooklyn, including Philadelphia")
  holds too, and an excluded value named "Top" is no ranking.
- A time exclusion ("signups not in June 2024", "excluding Jun. 25, 2024") holds instead of
  being read as the question's window, since Query IR has no window complement.
- For "excluding web", `plan` now drafts `IS DISTINCT FROM 'web'` instead of `= 'web'`, so
  such questions can be ready to execute. The Query IR contract lists `IS DISTINCT FROM` as a
  `where` operator; it was already accepted.
