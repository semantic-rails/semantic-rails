- The Architect MCP's `upsert_model` takes `calendar: true`, with an optional
  `calendar_id`, to make a model the package calendar: its entity becomes
  `kind: time` and not a query root, and may carry date dimensions, so
  `time.fill` works in packages authored through the MCP.
