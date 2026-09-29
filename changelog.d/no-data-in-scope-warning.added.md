- A query whose sum, count or distinct count reads `NULL` on every returned row, or that returns
  no rows and has no time window, now carries one `NO_DATA_IN_SCOPE` warning that names those
  outputs. A misspelled filter value used to read as a confident `0`; it now reads `NULL` with
  the warning. It needs no extra query, and a clipped (`truncated`) result never gets it.
- A query whose lowering skips the step that settles empty groups is refused with the stable
  code `EMPTY_GROUPS_UNSETTLED` instead of answering with a silent `NULL`.
