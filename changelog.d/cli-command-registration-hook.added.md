- Packages can add `semantic-rails` commands, or extend existing ones (extra arguments,
  a wrapped handler, new `import --from` sources and `export-contract --format` formats),
  through the `semantic_rails.cli` entry-point group and
  `semantic_rails.cli.registry.CommandRegistry`. A plugin from another distribution that
  fails to load is skipped with a warning; `SEMANTIC_RAILS_CLI_PLUGINS=0` turns plugins
  off.
