- The Architect MCP's `remove_object` removes a model, dimension, time, measure, metric, segment or
  foreign-key relationship in one parse-gated transaction, archiving the removed YAML. A model takes
  its entity and the relationships naming it along. A removal that would leave a metric naming what
  it removes is refused, and the report shows the `impact_project` summary and the files that still
  name a removed id. See [docs/ARCHITECT_MCP.md](docs/ARCHITECT_MCP.md).
