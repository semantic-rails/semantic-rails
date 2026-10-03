- `plan` no longer reports `ok` when its draft leaves out a question word that names something in
  the catalog. "Revenue by store, customer type and product type" drafted revenue by store alone,
  and a question about discounts could draft a measure described as "the charges that are not
  discounts", each with only a `PLAN_UNMATCHED_TERMS` warning. A word of the label or aliases of a
  measure, metric, dimension, entity, segment or time role, or of the last dotted part of its id
  or name outside its own namespaces, must now be consumed by the draft: by the label, aliases,
  id or name of an object it selects, a filter value, a time phrase it read, or as a framing word.
  A synonym, a typo, a namespace, a description or an object the draft doesn't select (a
  measure's entity included) never consumes one, so one catalog name can't stand in for another;
  "revenue from orders" is held back too, since Orders is a measure. Otherwise the plan is
  `low_confidence` with `why.code="PLAN_UNMATCHED_TERMS"` naming the words, and
  `why.details.dropped_groupings` when they sit in a grouping the question asks for; the draft
  stays in `best`. Descriptions and topics never account for a word in the warning any more, so
  the warning now names a word only a description holds; such a word, which no object's names
  hold, stays a warning.
