- `semantic_rails.embedding` exports the package authoring seams `ArchitectProject`,
  `ArchitectMutation`, `project_revision`, `ABSENT_PROJECT_REVISION` and `impact_report`, and
  the stateless Streamable HTTP handler `handle_streamable_http_request` with its
  `MCPHTTPResponse`. See "Serving MCP over HTTP" and "Package authoring" in
  [docs/EMBEDDING.md](docs/EMBEDDING.md).
- `handle_jsonrpc_message` and `handle_streamable_http_request` accept any adapter that
  satisfies the new `MCPAdapter` protocol, so a host's own adapter type-checks.
- `SemanticLayerMCPAdapter.replace_tool_handler(name, handler)` swaps the body of one tool on
  one adapter and keeps its argument validation, trusted request context and audit event. Use
  it instead of the private `_tool_handlers` mapping.
- [docs/EMBEDDING.md](docs/EMBEDDING.md) ends with a reference of every facade name and its
  call shape, checked against the facade by a test.
