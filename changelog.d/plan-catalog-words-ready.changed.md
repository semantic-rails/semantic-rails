- `plan` no longer reports `ok` when its draft leaves out a question word that names something in
  the catalog. "Revenue by store, customer type and product type" drafted revenue by store alone,
  and a question about discounts could draft a measure described as "the charges that are not
  discounts", each with only a `PLAN_UNMATCHED_TERMS` warning. A word in the id, name, label or
  aliases of a measure, metric, dimension, entity, segment or time role must now be used by the
  draft: by the names of an object it uses (a measure's entity and time role included), a filter
  value, a time phrase it read, or as a framing word. Otherwise the plan is `low_confidence` with
  `why.code="PLAN_UNMATCHED_TERMS"` naming the words, and `why.details.dropped_groupings` when they
  sit in a grouping the question asks for; the draft stays in `best`. Descriptions and topics never use a
  word any more, so the warning now names a word only a description holds; such a word, which no
  object's names hold, stays a warning.
- Exact catalog names take precedence over typo matching in `plan` readiness checks, and a
  namespace in another object's identifier no longer hides an unlabeled leaf name. A draft that
  drops those names stays `low_confidence` and identifies the omitted groupings.
