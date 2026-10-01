- A query whose sum, count or distinct count (or a sum or difference of them) reads `NULL` on
  every returned row, or that returns no rows with no time window and no metric filter, now
  carries one `NO_DATA_IN_SCOPE` warning that names those outputs. A misspelled filter value
  used to read as a confident `0`; it now reads `NULL` with the warning. A `prior_period`,
  ratio or rolling output never gets it. It needs no extra query, and a clipped (`truncated`)
  result never gets it.
- A query whose lowering skips the step that settles empty groups is refused with the stable
  code `EMPTY_GROUPS_UNSETTLED` instead of answering with a silent `NULL`.
