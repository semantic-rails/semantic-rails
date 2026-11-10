- The query MCP can be served without `plan`: set `SEMANTIC_RAILS_MCP_HIDE_PLAN=1` (or pass
  `hide_plan=True` to `SemanticLayerMCPAdapter`). `plan` is then neither listed nor callable, and
  the instructions, tool descriptions, prompts and hints describe discover, inspect, execute in
  `validate` mode, then execute. Unset, the server is unchanged.
