- `plan` holds every question that excludes values ("excluding", "except", "without", "not",
  "but not", "other than", "apart from", "aside from", "minus", "outside of", "all stores
  but"), whatever the draft carries: `negation_reversed` when an `=` or `IN` filter keeps an
  excluded value, otherwise `negation_unrealized`. This release doesn't answer exclusions yet.
  Before, a draft with `!=` or `NOT IN` could be ready to execute although it also dropped the
  rows with no recorded value, and a time exclusion ("signups not in June 2024") could be read
  as the question's window. The hold suggests asking for the breakdown by the excluded
  dimension instead (`ask_for_breakdown`), which shows each value and the rows with no value.
- The held draft excludes a value with `IS DISTINCT FROM`, which keeps rows with no recorded
  value ("signups excluding web" counts the signups with no channel), instead of `= 'web'`.
  A hand-written Query IR with that filter still runs. The Query IR contract lists
  `IS DISTINCT FROM` as a `where` operator; it was already accepted.
