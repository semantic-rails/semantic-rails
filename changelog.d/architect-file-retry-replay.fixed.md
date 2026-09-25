- A retried Architect `write_project_file` with `overwrite: false`, or a retried
  `archive_project_file`, now replays the first call's result instead of failing because the first
  call already wrote or archived the file.
