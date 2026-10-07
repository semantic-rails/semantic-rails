- Remove the optional MCP SDK stdio facade and the interface selector argument and environment
  setting. Use `SemanticLayerMCPAdapter`, the packaged stdio command, or the ASGI `/mcp` endpoint.
- Unknown MCP tools consistently return `UNKNOWN_MCP_TOOL` with the available tool names.
