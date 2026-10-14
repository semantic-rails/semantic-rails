- A `limit` with no `order_by` keeps the first rows by the `group_by` columns, then `period`,
  ascending with NULLs last, so the same call keeps the same rows.
