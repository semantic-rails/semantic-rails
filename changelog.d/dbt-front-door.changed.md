- A package inside a dbt project reads the DuckDB file `dbt build` writes: the `default_db` of
  a `seed.kind: external` package may use `..` to name a file inside that dbt project, with no
  environment variable. Other paths outside the package still fail to load.
- The Architect MCP's workspace root defaults to the directory it starts in, not the installed
  package's folder. `semantic-rails mcp setup --mcp architect` needs no package, and without
  `--client` it sets up every MCP client installed here (Claude Code included).
- `setup_project_dialog` reads a dbt project in the workspace: dbt answers the warehouse, the
  database path and the first model, the dialog asks only what is left, and it names the
  `import_dbt_project` call that brings in the rest. `create_project` writes an absolute
  `default_db` inside the workspace root relative to the package.
