- `plan` no longer calls a draft `ok` when its result lacks the part the question's shape asks
  for. "who", "which" or "list" needs a key dimension of the entity the clause lists in
  `group_by`: one value ("Who ordered last week?" returned the order count), a time grain
  ("List customers by month" returned monthly counts), a category or another entity ("Who are
  our customers by store?" returned store counts) no longer passes, and "who" naming no entity
  takes its rows only from the caller's `group_by`. "each" needs a row per item. A comparison
  ("compared with", "vs", "up or down") needs a prior-period select or a second, different
  select; a `group_by` only splits one value ("Orders by store last week compared with the
  week ?" returned last week alone). Several questions for a value need a select of their own
  each that names what the question asks about ("How many orders and how much revenue last
  week?" returned revenue alone, or the same revenue select twice). These are now
  `low_confidence` with a `PLAN_INTENT_COVERAGE_GAP` gap naming the missing part
  (`list_unrealized`, `each_unrealized`, `comparison_unrealized`,
  `multiple_questions_unrealized`).
- `plan`'s recovery hints no longer suggest asking again without the words a hold names
  (catalog names, groupings, numbers or unknown words): a question without them can be another
  one, and asking it that way was `ok` with the wrong answer. Only the "s" ending a contraction
  ("What's") is still offered, never another ending such as the "t" of "can't".
