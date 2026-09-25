- `semantic_rails.dev_cli` is removed. The human CLI lives in `semantic_rails.cli`
  (commands, reports and output) and `semantic_rails.repl`. `semantic_rails.cli` now exports
  only `main`: import the engine names it used to re-export, such as `Runtime` or `serve`,
  from their own modules. The `semantic-rails` command and `python -m semantic_rails` are
  unchanged.
