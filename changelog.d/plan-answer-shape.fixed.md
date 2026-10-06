- `plan` no longer calls a draft `ok` when its result lacks the part the question's shape asks
  for. "who", "which" or "list" needs the declared key of the entity the clause lists in
  `group_by`: one value ("Who ordered last week?" returned the order count), a time grain
  ("List customers by month" returned monthly counts), a category or another entity ("Who are
  our customers by store?" returned store counts) no longer passes, nor does a name alone,
  since a name can repeat ("List customers" grouped by Customer name returned one row for
  customers who share a name; "Which 3 stores had the most revenue last month?" grouped by
  Store name) or any other dimension of the entity. A name may sit beside the key. "who"
  naming no entity takes its rows only from the caller's `group_by`. "each" needs a row per
  item. A comparison ("compared with", "vs", "up or down") needs a prior-period select; a
  second select, even the same measure spelled another way, or a `group_by` doesn't show what
  is compared ("Orders by store last week compared with the week ?" returned last week alone,
  or the same order count twice; "Food revenue vs drink revenue last month" is also held).
  Several questions for a value need a select of their own
  each that names what the question asks about ("How many orders and how much revenue last
  week?" returned revenue alone, or the same revenue select twice). These are now
  `low_confidence` with a `PLAN_INTENT_COVERAGE_GAP` gap naming the missing part
  (`list_unrealized`, `each_unrealized`, `comparison_unrealized`,
  `multiple_questions_unrealized`).
- `plan`'s recovery hints no longer suggest asking again without the words a hold names
  (catalog names, groupings, numbers or unknown words): a question without them can be another
  one, and asking it that way was `ok` with the wrong answer. Only the "s" ending a contraction
  ("What's") is still offered, never another ending such as the "t" of "can't".
