- `plan` no longer calls a one-value draft `ok` when the question asks for more: rows for
  "who" or "which" ("Who ordered last week?" returned the order count), a row per item for
  "each", two values or more for "compared with", "vs" or "up or down", and a select for each
  of several questions ("How many orders and how much revenue last week?" returned revenue
  alone). These are now `low_confidence` with a `PLAN_INTENT_COVERAGE_GAP` gap naming the
  missing part (`list_unrealized`, `each_unrealized`, `comparison_unrealized`,
  `multiple_questions_unrealized`).
- `plan`'s recovery hints no longer suggest asking again without the words a hold names
  (catalog names, groupings, numbers or unknown words): a question without them can be another
  one, and asking it that way was `ok` with the wrong answer. Only the end of a contraction
  ("s" in "What's") is still offered.
