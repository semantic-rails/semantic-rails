- Architect MCP writes, `validate_project`, `project_status` and `promotion_check` answer with a
  summary an MCP client shows whole: warning counts by code with up to ten warnings (each code's
  first before any repeat), the route
  census's counts with its first undecided pairs, failing checks only, package files without
  transaction receipts, and file diffs without their full contents. Errors, revisions and
  refusal details (such as `CONFIG_CONFLICT`'s `retry`) are unchanged; pass `full_report: true`
  for the whole report. A dry run's parse messages now name the project rather than the
  temporary copy it was checked in.
