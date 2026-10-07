- Remove the optional MCP SDK stdio facade, the interface selector environment setting, and
  the `from_package`/`from_path` interface arguments. The adapter constructor keeps `interface=`.
  Use `SemanticLayerMCPAdapter`, the packaged stdio command, or the ASGI `/mcp` endpoint.
- Unknown MCP tools consistently return `UNKNOWN_MCP_TOOL` with the available tool names.
