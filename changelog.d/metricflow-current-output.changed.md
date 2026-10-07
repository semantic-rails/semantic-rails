- MetricFlow imports always write strict, parse-checked packages and expose only
  explicitly defined metrics. Rename `--schema-strict` to `--keep-schema` to
  preserve dbt relation schemas and read its existing database.
- MetricFlow dimension column expressions now map to `column:`; unsupported
  dimension expressions and parameterized percentile measures warn and are skipped.
- MetricFlow conversion metrics are skipped with a warning. Metrics that depend
  on skipped metrics or dimensions are skipped too, preserving a loadable package
  when independent metrics remain.
- A MetricFlow derived expression mf2sr can't translate exactly is skipped with a
  warning instead of being written as an aggregate over its first input.
  `NULLIF(x, 0)` is accepted only as a division's denominator.
