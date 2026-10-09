- Loading a package refuses more authored input that the loader would silently drop, each
  with `INVALID_CONFIG` and one line per error in `validate-config`. In a directory package: a
  block declared in `package.yml` and in its own file (a `defaults:` block beside
  `defaults.yml`, which replaced it), an object id defined twice (a model in `package.yml` and
  under `models/`, or in two files), a root YAML file or root directory of YAML that nothing
  reads, such as a `policies/` directory (write its rows in `policies.yml`), a root
  `tests.yml` or `examples.yml` (write the entries under `tests/` or `examples/`) or a
  `notes.yml`, and a directory symlink the loader never followed that could hide package input
  (copy the directory or link its files): one at or under `models/`, `relations/`, `metrics/`,
  `segments/`, `examples/` or `tests/`, or one holding a `.yml` or `.yaml` file, a directory
  symlink or an unreadable directory. A link to a folder of data files, such as `data/`, still
  loads, and Architect writes and `semantic-rails project upgrade` accept it; names starting
  with `_` or `.` stay ignored. In every layout: a rollup
  `columns:` entry whose name binds no measure, dimension or key column (the measure it meant
  read the column named after it instead), including one named by an `id:` that `as:`
  replaces, and an `accumulation:` key other than `kind` and `snapshot`.
