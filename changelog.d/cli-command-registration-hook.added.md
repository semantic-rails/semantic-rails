- Other installed packages can add `semantic-rails` commands: each entry point in the
  `semantic_rails.cli` group receives the top-level subparsers and adds its commands. A
  plugin that fails to load or reuses a command name is skipped with a warning;
  `SEMANTIC_RAILS_CLI_PLUGINS=0` turns plugins off.
