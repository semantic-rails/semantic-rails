- Retain each named value in a compound filter phrase, including in ranked
  questions.
- Preserve distinct requested groupings in catalog fallback, deduplicating
  only identical dimension IDs and retaining the user's discovery terms.
- Ask for clarification whenever the caller passes `group_by` and the draft
  adds a grouping dimension the caller didn't pass: the plan keeps both
  groupings and returns `low_confidence` until `group_by` names every intended
  dimension ID.
