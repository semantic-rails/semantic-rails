- `semantic_rails.dev_cli` is removed, with no compatibility shim. Import its names from
  their own modules: the developer commands (`cmd_*`, `add_developer_cli`) from
  `semantic_rails.cli.commands.project`; the report builders (`ask_report`,
  `project_status_report`, `setup_report` and the rest) from `semantic_rails.cli.reports`;
  `create_project_report` from `semantic_rails.cli.scaffold`; `cmd_setup_interactive` from
  `semantic_rails.cli.setup_wizard`; `describe_query` from `semantic_rails.cli.interpretation`;
  `DEMO_PACKAGE_ID` and `default_package_ref` from `semantic_rails.cli.common`; and
  `run_interactive_shell` from `semantic_rails.repl.shell`.
- `semantic_rails.cli` now exports only `main`. The engine names it re-exported in 0.2.1 are
  removed. Import them from their own modules: `Runtime` from `semantic_rails.runtime`;
  `SemanticLayerError` from `semantic_rails.errors`; `serve` from `semantic_rails.api`;
  `serve_mcp_http` and `serve_mcp_stdio` as `serve_http` and `serve_stdio` from
  `semantic_rails.mcp_server`; `SemanticLayerMCPAdapter` from `semantic_rails.mcp`;
  `catalog_payload`, `discover_payload`, `inspect_payload`, `build_options_payload` and
  `valid_values_payload` from `semantic_rails.metadata`; `plan_payload` from
  `semantic_rails.planner`; `parse_config_report`, `validate_config_report` and
  `resolve_package_reference` from `semantic_rails.config_validation`; `list_package_ids`
  from `semantic_rails.config`; `export_semantic_contract` from `semantic_rails.contracts`;
  `exception_issue` from `semantic_rails.diagnostics`; and `check_package_report`,
  `build_package_artifact_report`, `diff_package_report`, `impact_report`,
  `promote_package_report`, `run_examples_report` and `run_package_tests_report` from
  `semantic_rails.package_tools`. The CLI command handlers (`cmd_*`) and `MCP_REQUIRED_TOOLS`
  are in the `semantic_rails.cli.commands` modules. The `semantic-rails` command,
  `python -m semantic_rails` and `python -m semantic_rails.cli` are unchanged.
