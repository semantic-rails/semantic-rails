- **Agents.** The query MCP's `execute` description says which `time.temporal_role` to give:
  with `time`, set it to the role the question names, else to the selected ids'
  `default_temporal_role` from `inspect`; a measure whose card lists no time role takes no
  time, so filter or group on a date dimension instead.
- **Agents.** `SEMANTIC_CAVEAT_APPLIED` carries the caveat's `so_what` at every verbosity,
  `minimal` included (the query MCP's default), so an agent reading the smallest answer still
  sees what the package owner says to do. `owner` still comes at `compact` and `full`. See
  [`caveats.yml`](docs/PACKAGE_AUTHORING.md#caveatsyml).
