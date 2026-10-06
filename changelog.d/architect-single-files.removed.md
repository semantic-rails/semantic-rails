- Removed Architect `write_project_file` and `archive_project_file` and the Python service
  methods `write_file` and `archive_file`. This is a breaking change in 0.x; use
  `write_project_files` (Python: `write_files`) with a one-entry list for a single file.
