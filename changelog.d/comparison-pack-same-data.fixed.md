- Every layer in the semantic-layer comparison pack now reads the same `comparison_*` views.
  MetricFlow, Malloy and KtX were re-run with pinned environments. Cube 1.6.32 can't be
  reinstalled until its captured lockfile's advisories are resolved, so the SQL it generated is
  re-executed on the same data instead. Each run records a fingerprint of the dataset it read.
  A capture made on other data, such as the Snowflake one, is reported as stale rather than as
  a mismatch.
