- Removed duplicate query MCP error payloads and error recovery hints: `error` now
  contains only the first issue's code and message; full issues and their hints live
  in `errors`. Minimal responses omit mixed-grain relationship analysis and rewrite
  analysis/path details; request `compact` or `full` for those details.
- Removed copies of an issue's `recovery_hints` from its `details` across runtime,
  HTTP, CLI, and MCP responses; hints remain on the issue itself.
