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
  `NULLIF(x, 0)` is accepted only as a division's denominator, and a formula with
  `--`, `/*`, `#`, `;` or a literal other than a plain decimal is skipped.
- MetricFlow counts translate exactly or are skipped with a warning. The counted
  column is `expr`, or the measure's name without one, never the row count. A
  `count` is `COUNT(col)` of that column; a `count_distinct` is
  `COUNT(DISTINCT col)` when a graph entity is keyed on the column or the model
  declares it as a foreign key, and is skipped otherwise. A `count_distinct` of a
  constant and an unknown `agg` (once read as `sum`) are skipped too. A row count
  (`expr: "1"` with `sum` or `count`) is translated only when the model's own
  entity is declared `type: primary`; over a `unique` entity or a bare
  `primary_entity:` it is skipped, as is any other aggregation of a constant.
