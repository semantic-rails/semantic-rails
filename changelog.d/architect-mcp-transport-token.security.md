- The Architect MCP's `sse` and `streamable-http` transports now require a
  bearer token (`--token-file`, `SEMANTIC_RAILS_ARCHITECT_TOKEN_FILE` or
  `SEMANTIC_RAILS_ARCHITECT_TOKEN`: at least 32 characters) and refuse to start
  without one; set it for both the server and its clients. The Host check also
  accepts the address given to `--host` and host names without a port, and
  `mcp_client_config` returns an `Authorization` header template. The stdio
  transport is unchanged.
