- The query MCP states its workflow once, in the server `instructions` returned by
  `initialize`, instead of as "loop position" prose on every tool. Tool descriptions say what
  each tool does, when to use it and its one gotcha, and no longer contradict each other about
  whether `validate` and `compile` must run before `execute` (they are optional dry runs).
  `request_id` and `policy_context` are still accepted by every tool but no longer advertised on
  each schema. The model-visible `tools/list` shrinks by about a quarter.
