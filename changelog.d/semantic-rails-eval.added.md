- `semantic-rails eval` runs a package's `tests/answers.yml` against its warehouse and fails CI
  before a model change alters a governed number. An entry freezes the rows a call must
  return, or the code a call must refuse with. A wrong row, or a number for a call that must
  refuse, fails (exit 1); a refusal where rows were expected is a miss (exit 2, or 1 with
  `--strict`). `--json` prints the report. See [docs/EVAL.md](docs/EVAL.md).
