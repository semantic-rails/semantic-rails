- `plan` no longer changes what a question asks when it reads a time or a name. "Revenue from
  12:00 to 13:00 on 15 March 2017" drafted the whole day and reported `ok`. `plan` resolves days
  and coarser windows only, under one rule: a draft is `ok` only if every number, spelled-out
  number and clock or zone word in the question ("9", "nine", "o'clock", "hour", "noon", "UTC",
  "EST", "ET", "Europe/Berlin") sits inside the text of a construct the draft carries (the date
  or window, a limit, threshold or percentile the question states, a filter value, an object's
  name), never because its value equals one: "at 1930" is not a year. Otherwise the plan is
  `low_confidence` with
  `PLAN_UNMATCHED_TERMS`, the leftover words in `why.details.terms` and no `next.ready_for`, so
  "between 9 and 17", "from nine to five", "at 14h30" and "in UTC" beside a date are not ready.
  Ordinary words such as "min", "net" and "EBIT" are not clock or zone words. A number range the
  draft doesn't carry ("aged 25-34", "2 to 5 orders") is named the same way; it is never read
  as an hour. A window shorter than a day ("last 24 hours", "past hour", "last 30 minutes") is
  `TIME_WINDOW_UNRESOLVED` with no query, not a query over all time. A zone written as an
  ordinary word ("Pacific time", "local time") is not recognised on its own. To ask for an hour
  range, pass `query.time.start` and `query.time.end` as end-exclusive ISO timestamps in the
  temporal role's time zone. A window restated beside
  itself ("Q1 2017 (January 1 to March 31, 2017)") is one window; two that differ, or the same
  one beside another condition ("revenue in 2017 from customers who signed up in 2017"), are
  named in `why.details.conflicting_phrases`. "Year 2017" and "calendar year 2017" resolve;
  "financial year 2017" and "model year 2017" are reported. A range's spoken last day is
  stated as included in `assumptions`, which `ask` prints with its warnings.
- `plan` reads a measure the question names in full ahead of a shorter one that shares a word
  with it, for a measure by a dimension: "item revenue" is Item revenue, not Revenue, while
  "large order revenue" stays revenue. A ratio or growth question keeps its metric target. A
  question that lists several
  measures ("item revenue and orders in Q1 2017", "revenue, orders and gross profit") is
  `low_confidence` with a `multiple_subjects_unrealized` gap when the draft leaves one out,
  also when a time phrase follows the list.
- `PLAN_UNMATCHED_TERMS` no longer names verbs and function words such as "dated", "placed",
  "only", "while" and "using". Two or more names the catalog doesn't have after "for", "from", "of"
  or "with" ("for tangaroo and vanilla ice") make the plan `low_confidence` instead of a
  warning, since the draft dropped a filter.
