- Before an operational `validate` (runtime, examples, tests or full) on a DuckDB package, the
  REPL names the selected database file. It explains when a missing seeded file may be created,
  and that an existing file is never rebuilt or replaced. External missing files and broken
  links are reported without creation. Validation output prints a repeated error once with a count.
