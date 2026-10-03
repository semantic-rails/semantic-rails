- Managed MCP startup reports OS-assigned-port bind failures as configuration
  errors without spawning a process or registering a server. Configuration
  conflicts include the assigned port, and start help explains port zero.
- MCP HTTP servers consume the inherited socket-fd environment variable at
  startup so child processes do not receive stale socket-fd configuration.
