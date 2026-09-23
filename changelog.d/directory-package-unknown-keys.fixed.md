- Package validation now rejects unknown keys in the metrics and segments of
  directory packages, in every layout the loader reads (files under `metrics/`
  and `segments/`, root `metrics.yml` and `segments.yml`, and `package.yml`), as
  it already did for single-file packages and for models. These keys used to
  pass silently, and the loader ignored them: a typo such as `valeu_type`, or a
  segment `where` written outside `membership:`, which the segment then
  ignored. Unknown keys inside a segment's `membership:` block are now rejected
  in every package, and `filters` or `dimension_filters` point to
  `membership.where`. Directory-package metrics also get the checks single-file
  packages already had: an unknown `kind:` now fails validation, and a missing
  required field gets a clearer error. A package that is not `schema_strict` now
  fails on
  `preferred_filter_ops` on a metric, or on `clock_variants`, `comparison_peers`
  or `preferred_filter_ops` on a segment, as a single-file package already did.
  `mf2sr` writes `grain_to_date` on a cumulative metric, which the loader
  ignored, computing an all-time running total, so such a translated package
  now fails validation: rewrite the metric as `kind: period_to_date`.
