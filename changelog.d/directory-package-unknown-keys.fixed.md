- Package validation now rejects unknown keys in the metric and segment files of
  directory packages, as it already did for single-file packages and for models.
  These keys used to pass silently, and the loader ignored them: a typo such as
  `valeu_type`, or a segment `where` written outside `membership:`, which made
  the segment select every member. Unknown keys inside a segment's `membership:`
  block are now rejected in every package, and `dimension_filters` or `filters`
  there point to `membership.where`.
