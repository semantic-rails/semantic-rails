- A `limit` with no `order_by` keeps the first rows by the `group_by` columns, ascending with
  NULLs last, so the same call keeps the same rows. With `time.grain` it keeps the series order,
  `period` first, as before.
