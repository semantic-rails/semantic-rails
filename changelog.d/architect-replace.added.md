- The Architect MCP's `upsert_model`, `upsert_metric` and `upsert_segment` take `replace: true`,
  which rewrites the object from the arguments instead of merging into it. A model keeps its id,
  entity references and calendar binding, and the report lists what the rewrite dropped in
  `dropped_fields`. A metric or segment keeps its public id; `ArchitectProject.upsert_metric`'s
  `replace` used to drop it. `upsert_model` also takes `label` and refuses fact models. See
  [docs/ARCHITECT_MCP.md](docs/ARCHITECT_MCP.md).
