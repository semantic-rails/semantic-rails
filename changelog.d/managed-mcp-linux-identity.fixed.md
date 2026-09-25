- On Linux, `semantic-rails mcp start`, `mcp status` and `mcp stop` identify a managed server
  by the kernel's process start tick instead of `ps`'s start time, which can shift by a second
  when the system clock is stepped. `mcp start` no longer reports `failed_to_start` for a
  healthy server, and `mcp stop` no longer refuses to stop one. A server started by an earlier
  version shows as unverified until it is stopped and started again.
