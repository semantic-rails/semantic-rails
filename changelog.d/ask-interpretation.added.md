- `semantic-rails ask` prints "Interpreted as: ...", a plain restatement of what the
  executed query computes: measures and metrics with their aggregation, grouping, time grain
  and window, filters and row limits. `--json` adds `interpretation`. If a question was
  misread, the restatement shows it instead of the answer looking fine.
