- Removed the rollup certification API and provider hook. Packages declaring
  `requires_certification` now fail to load with `INVALID_CONFIG` for an unknown key.
- Removed the single-file `init` scaffold and its `--output`, `--single-file`, and
  `--split` options. Use `semantic-rails init <name>` for a directory package;
  existing single-file packages remain supported by the loader.
- Removed CLI command plugins registered through the `semantic_rails.cli` entry
  point group and the `SEMANTIC_RAILS_CLI_PLUGINS` setting.
