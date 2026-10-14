- A relationship without `cardinality:` whose `target:` columns don't cover the target
  entity's key now fails loading with `INVALID_CONFIG`, instead of reading as many-to-one and
  counting a row once per matching target row. Join on the key, or declare `cardinality:`
  (`many_to_many` when the values repeat). A `via:` on part of a composite source key now
  reads as a foreign key, so the reverse hop no longer reads as one-to-one.
