- The query MCP states its workflow once, in the server `instructions` returned by
  `initialize`, instead of as "loop position" prose on every tool. Tool descriptions say what
  each tool does, when to use it and its one gotcha, and no longer contradict each other about
  whether `validate` and `compile` must run before `execute` (they are optional dry runs).
  The v1 tool schemas continue to advertise and accept `request_id` and `policy_context`.
  Workflow guidance moves to server instructions, keeping tool descriptions shorter.
