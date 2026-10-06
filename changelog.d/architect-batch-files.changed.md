- Architect `write_project_files` applies writes and archives in one atomic package change.
  It takes `project_path`, `files` entries with `path` and either `content` plus optional
  `overwrite`, or `archive: true`, required `expected_revision` and `idempotency_key`, and
  optional `reason` and `dry_run`. The whole package validates after every file is staged.
