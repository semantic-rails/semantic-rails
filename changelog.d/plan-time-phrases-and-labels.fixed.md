- `plan` no longer changes what a question asks when it reads a time or a name. "Revenue from
  12:00 to 13:00 UTC on 15 March 2017" drafted the whole day and reported `ok`; a time of day
  now resolves to a timestamp window (end exclusive, zone stated in the new `assumptions`),
  and a lone time, a range across midnight or a zone other than UTC is reported as
  `TIME_WINDOW_UNRESOLVED` with no query. A window stated twice the same way ("Q1 2017 (January
  1 to March 31, 2017)") is one window, two that differ are named in
  `why.details.conflicting_phrases`, and "year 2017" and "calendar year 2017" resolve. A
  range's spoken last day is stated as included in `assumptions`.
- `plan` reads a measure the question names in full ahead of a shorter one that shares a word
  with it: "item revenue" is Item revenue, not Revenue. A question that lists several
  measures ("item revenue and orders in Q1 2017", "revenue, orders and gross profit") is
  `low_confidence` with a `multiple_subjects_unrealized` gap when the draft leaves one out,
  also when a time phrase follows the list.
- `PLAN_UNMATCHED_TERMS` no longer names verbs and function words such as "dated", "placed",
  "only", "while" and "using", and names a number the draft doesn't carry ("2 or more orders"
  lists "2" and "more"). Two or more names the catalog doesn't have after "for", "from", "of"
  or "with" ("for tangaroo and vanilla ice") make the plan `low_confidence` instead of a
  warning, since the draft dropped a filter.
