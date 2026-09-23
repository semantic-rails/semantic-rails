- Under `schema_strict: true`, a metric that declares `value_type: number` is
  now accepted. Ratio and derived metrics with it were rejected as having an
  "implicit" value type. A metric whose `value_type` is missing, `null` or
  empty is now rejected in every layout the loader reads, single-file packages
  included. Before, only metric files under `metrics/` were checked for a
  missing value type. A directory package that writes `schema_version: "1"`,
  which the loader accepts, now gets the same directory checks as one that
  writes `1`. Before, it skipped them.
