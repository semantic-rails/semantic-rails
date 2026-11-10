- The docs name `plan`'s argument `query` (not `partial_query`), use one local port (8091),
  count five query tools (six with segments), and show `record_route_decision` taking
  `decisions`. The Architect tool table in [Architect MCP](docs/ARCHITECT_MCP.md#tool-surface) is
  generated from the server's own tool list, and CI fails when they differ.
- `catalog`, `discover`, `inspect` and `build-options` refuse a `--verbosity` they don't
  support instead of ignoring it.
