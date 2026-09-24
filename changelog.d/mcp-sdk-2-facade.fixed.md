- `create_optional_fastmcp_server` selects `MCPServer` when the MCP Python SDK 2.x module is
  present, or `FastMCP` on the installed 1.x SDK. The 2.x branch is covered by a simulated
  module test; an SDK 2.x install has not been qualified for the full package.
