- Hold plans that leave a caller-visible dimension the question names neither grouped nor
  pinned to one value by the query's `where`, in any phrasing, or that leave an entity named
  in a level or grain question without a grouped stand-in. Names are read with spaces, underscores and any case,
  including `customer_type`, `_customer_type` and `customer_type_`; a value word inside the name
  or in the measure's label no longer exempts it. Hidden objects are never read, and existing
  drafts and readiness holds are preserved.
- Hold plans when a filter value is matched inside a declared grouping name in the question,
  whatever words surround the name and even with complete groupings; retain the draft for
  inspection and name the filter in `why.details.filter_inside_grouping`.
