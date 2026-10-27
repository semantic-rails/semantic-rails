- Architect's `suggest_one_per_key` proposes reading a table with no key of its own one row per a
  key it repeats (a room listed once per load, a catalog item once per copy), so other tables can
  look it up. From the data, each candidate key shows its rows, rows without a key, distinct
  keys, keys with several rows, and which columns are fixed or vary within a key. A proposal
  states its coverage ("5 of 6 rows have a key; 1 row has none and is still counted by the
  row-grain model") and returns draft `upsert_model` arguments (`one_per_key: true`, the fixed
  columns, a fixed key column as the parent entity) and, with `project_path`, the
  `upsert_relationship` that links the table's row-grain model to it. It writes nothing: apply the
  drafts with `dry_run`, then `expected_revision`. A table keyed by one column of its own is
  pointed to `suggest_model` instead, and a table whose columns all vary within every key gets no
  proposal, with the reason.
