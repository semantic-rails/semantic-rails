- `plan`'s `PLAN_SUBJECT_UNMATCHED` no longer says "Nothing in the catalog mentions them, so the
  package may not hold this" after checking only measures and metrics. It now checks every kind
  the caller can see: words that dimensions, time roles, segments or entities mention are named
  in `why.details.other_matches` with their `kind` ("These words match dimension Customer type
  (dimension.customer_type), which can group or filter but isn't a measure"), and the status is
  `low_confidence` instead of `unrealizable`. The message says nothing in the catalog mentions
  the words only when no visible object of any kind does, and no longer adds that the package
  may not hold them. An object hidden from the caller is never matched. The recovery hint points
  to `discover`.
