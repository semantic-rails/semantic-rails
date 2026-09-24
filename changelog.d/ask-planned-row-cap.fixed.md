- `semantic-rails ask` respects a row cap in the planned query alongside `--limit`,
  reports it as `result.planned_row_limit`, and no longer describes it as liftable with
  `--limit 0`. `semantic-rails --help` clarifies which commands require a package.
