- A relationship without `cardinality:` whose `target:` columns don't hold the target entity's
  whole key now fails loading with `INVALID_CONFIG`, instead of reading as many-to-one and
  counting a row once per matching target row. Join on the key, or declare `cardinality:`
  (`many_to_many` when the values repeat). A side reads as unique only when its columns hold
  its entity's whole key and that key is declared `primary` or `unique`, so a `via:` on part of
  a composite key, or on a `natural` or `foreign` key, no longer reads as one-to-one. Key
  columns match in any order; a join column that differs from a key column only in case now
  fails loading, since warehouses disagree on whether the two spellings name one column.
