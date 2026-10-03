- Contextual metric predicates grouped by an attribute of their input rows now
  count within that attribute's values, including NULL groups. Distributions,
  including those in derived metrics, refuse metric filters whose grouping grain
  cannot be preserved instead of returning dropped or resurrected groups.
