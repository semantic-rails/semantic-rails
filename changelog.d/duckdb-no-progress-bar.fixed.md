- DuckDB and MotherDuck connections no longer draw DuckDB's progress bar on standard output
  when a statement takes longer than 2 seconds. On a server, the bar went into the log stream,
  and on the MCP stdio server it went into the JSON-RPC stream.
