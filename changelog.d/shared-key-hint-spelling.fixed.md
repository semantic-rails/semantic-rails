- When two models' keys spell the column they share with different capitals (`instructor_key`
  on one, `INSTRUCTOR_KEY` on the other), the `PATH_NOT_FOUND` author hint now spells `via:` as
  the start's key does and `target:` as the target's does, so the relationship row it suggests
  loads as written. Before, it used the target's spelling for both, and the row failed to load.
  `details.shared_key_columns_on_start` gives the start's spelling when it differs.
