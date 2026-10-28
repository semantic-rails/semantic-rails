- The query MCP can be served without `plan`, so agents build each call from what `discover`,
  `inspect` and `valid-values` return: pass `--no-plan` to `semantic-rails mcp stdio` or
  `semantic-rails serve`, set `SEMANTIC_RAILS_MCP_PLAN=off`, or create the adapter with
  `SemanticLayerMCPAdapter(runtime, plan=False)`. The tool list, instructions, prompts and
  recovery hints then never mention `plan`; a call to it is refused with a hint that points at
  `discover`, `inspect` and `execute`. Unset, `plan` is served as before. See
  [Serving without plan](docs/MCP_INTERFACE.md#serving-without-plan).
