- Loading a package refuses more authored input that the loader would silently drop, each
  with `INVALID_CONFIG` and one line per error in `validate-config`. In a directory package: a
  block declared in `package.yml` and in its own file (a `defaults:` block beside
  `defaults.yml`, which replaced it), an object id defined twice (a model in `package.yml` and
  under `models/`, or in two files), and a root YAML file or root directory of YAML that
  nothing reads, such as a `policies/` directory (write its rows in `policies.yml`) or a
  `notes.yml`; names starting with `_` or `.` stay ignored. In every layout: a rollup
  `columns:` entry whose name binds no measure, dimension or key column (the measure it meant
  read the column named after it instead), and an `accumulation:` key other than `kind` and
  `snapshot`.
