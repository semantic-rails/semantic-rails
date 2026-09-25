- Architect and REPL model edits read package YAML with the YAML 1.2 rules the loader uses, so
  unquoted `no`, `on`, `yes` and `off` in an existing model stay strings instead of being
  rewritten as `false` and `true`.
