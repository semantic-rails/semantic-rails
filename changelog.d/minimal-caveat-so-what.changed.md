- **Agents.** The query MCP's `execute` description says when to give `time.temporal_role`:
  leave it out when the selected ids share one `default_temporal_role`, else give one in each
  id's `compatible_temporal_roles`; an id with none takes no time, so filter or group on a date
  dimension instead.
- **Agents.** `SEMANTIC_CAVEAT_APPLIED` carries the caveat's `so_what` at every verbosity,
  `minimal` included (the query MCP's default), so an agent reading the smallest answer still
  sees what the package owner says to do. `owner` still comes at `compact` and `full`. See
  [`caveats.yml`](docs/PACKAGE_AUTHORING.md#caveatsyml).
