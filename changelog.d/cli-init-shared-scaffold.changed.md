- `init <name>`, `project new` and `setup --interactive` now write the same starter package
  as the Architect MCP's `create_project`, with the same files and object ids: an
  `<entity>_count` metric beside `total_amount` in `metrics/core.yml`, one example, one
  package test and the shared `.gitignore`. The starter model's label is the plural entity
  name (`Events`, formerly `Event events`), and its `event_type` dimension has no `domain`.
