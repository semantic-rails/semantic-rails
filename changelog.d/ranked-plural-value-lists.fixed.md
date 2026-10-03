- Plan ranked questions with singular or regular plural grouping names and
  retain each named value in a compound filter phrase.
- Preserve distinct requested groupings in catalog fallback, deduplicating
  only identical dimension IDs and retaining the user's discovery terms.
- Ask for clarification when a plural grouping read as its singular matches
  several dimensions reachable from the measure: unless the draft groups by
  the one match on the measure's own entity, the plan returns `low_confidence`
  with an `ambiguous_grouping` gap listing the matching dimension IDs. Plurals
  the planner already reads as its own words, such as "orders", aren't folded.
- Ask for clarification whenever the caller passes `group_by` and the draft
  adds a grouping dimension the caller didn't pass: the plan keeps both
  groupings and returns `low_confidence` until `group_by` names every intended
  dimension ID.
