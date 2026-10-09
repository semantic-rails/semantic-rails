- `plan` drafts the metric a question names by a whole synonym or label even when a measure
  is also named elsewhere in the question with only words of that metric's own names ("How
  many accounts moved to a bigger plan last week?" beside a measure "Accounts (all
  segments)"); before, the measure was drafted and held.
- `plan` answers every single-subject draft over a measure with the one metric that governs
  it, including a catalog-fallback draft, and when that metric is the filtered aggregate
  inside `COALESCE(<aggregate>, 0)` or the question has no time words; before, these drafts
  were held with `governed_metric_unrealized` naming that metric. A part of a compound
  question and a semi-additive measure still swap only to the bare filtered aggregate (for a
  semi-additive measure, a plain aggregate with a `filter`, not a `scoped_aggregate`), and a
  compound part only on the metric's clock.
