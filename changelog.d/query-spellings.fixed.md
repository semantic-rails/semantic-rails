- Accept unambiguous MCP query spellings and report their normalization; return actionable
  unknown-dimension errors for list queries and an explicit live-lookup step for undeclared
  value domains. Accept route clarification options and their unambiguous ids, and show
  conditional date arithmetic in execution guidance.
- Share select-dimension shorthand and its normalization warning across query surfaces;
  refuse conflicting targets and wrapped dimensions beside other grouping dimensions.
  Keep MCP preprocessing inside the structured error boundary and refuse excessive nesting.
- Resolve each nested MCP tool call's trusted context and error envelope independently,
  restoring the outer context when it returns.
- Remove duplicate `recovery_hints[].clarification`; the clarification remains in error
  details. Valid-values returns `ok: false` and `status: needs_live_query` without a declared
  domain unless live lookup is enabled; only MCP adds a `next_call` tool retry.
