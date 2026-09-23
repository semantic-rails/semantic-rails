- The CLI no longer answers from the bundled `jaffle_shop` sample package when you haven't
  chosen a package. Without `--package`, `--path`, a package directory or a local profile,
  commands stop and list the ways to choose one; JSON output reports `INVALID_CONFIG` with
  `details.reason: "no_package_selected"`. Scripts that relied on the old fallback should
  pass `--package jaffle_shop`. At an interactive terminal, `ask`, `ls`,
  `project status|validate`, `repl` and bare `semantic-rails` first offer the sample package
  (default No). `ask`, `ls`, `project status|validate`, `mcp setup` and the REPL label a
  bundled package as sample data (`package.bundled` in JSON).
