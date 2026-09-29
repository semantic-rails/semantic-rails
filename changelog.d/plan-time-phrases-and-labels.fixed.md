- `plan` no longer changes what a question asks when it reads a time or a name. "Revenue from
  12:00 to 13:00 on 15 March 2017" drafted the whole day and reported `ok`; a time of day
  now resolves to a timestamp window (end exclusive, the role's time zone stated in the new
  `assumptions`), and a lone time, a range across midnight or any time zone written after it
  ("UTC", "EST", "(UTC+2)", "Pacific time") is reported as `TIME_WINDOW_UNRESOLVED` with no
  query. A window restated beside itself ("Q1 2017 (January 1 to March 31, 2017)") is one
  window; two that differ, or the same one beside another condition ("revenue in 2017 from
  customers who signed up in 2017"), are named in `why.details.conflicting_phrases`. "Year
  2017" and "calendar year 2017" resolve; "financial year 2017" and "model year 2017" are
  reported. A
  range's spoken last day is stated as included in `assumptions`, which `ask` prints with its
  warnings.
- `plan` reads a measure the question names in full ahead of a shorter one that shares a word
  with it, for a measure by a dimension: "item revenue" is Item revenue, not Revenue, while
  "large order revenue" stays revenue. A question that lists several
  measures ("item revenue and orders in Q1 2017", "revenue, orders and gross profit") is
  `low_confidence` with a `multiple_subjects_unrealized` gap when the draft leaves one out,
  also when a time phrase follows the list.
- `PLAN_UNMATCHED_TERMS` no longer names verbs and function words such as "dated", "placed",
  "only", "while" and "using", and names a number the draft doesn't carry ("2 or more orders"
  lists "2" and "more"). Two or more names the catalog doesn't have after "for", "from", "of"
  or "with" ("for tangaroo and vanilla ice") make the plan `low_confidence` instead of a
  warning, since the draft dropped a filter.
