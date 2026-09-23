- The CLI no longer answers from the bundled `jaffle_shop` sample package when you haven't
  chosen a package. Without `--package`, `--path`, a package directory or a local profile,
  commands stop and list the ways to choose one; JSON output reports `INVALID_CONFIG` with
  `details.reason: "no_package_selected"`. At an interactive terminal, `ask`, `ls`,
  `project status|validate`, `repl` and bare `semantic-rails` first offer the sample package
  (default No), and answers from a bundled package are labeled as sample data.
