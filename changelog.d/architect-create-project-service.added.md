- One project scaffold, `architect_service.create_project(path, ProjectSpec)`,
  shared by the Architect MCP and (next) the CLI and REPL. It is
  warehouse-aware: DuckDB packages get a starter CSV seed or read a database
  another tool builds (`data: external`, for dbt), and other warehouses get a
  `connection` block. Every package is strict and ships a `.gitignore`. The
  Architect MCP's `create_project` gains `warehouse`, `data`, `default_db`,
  `connection_*` and `dimension_column`, and `setup_project_dialog` asks for
  the warehouse and connection. A directory without authored files (one that
  holds only the warehouse dbt built) now has revision `absent`, so a package
  can be created in it.
