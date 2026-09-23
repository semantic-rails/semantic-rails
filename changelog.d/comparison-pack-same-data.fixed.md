- Every layer in the semantic-layer comparison pack now reads the same `comparison_*` views,
  and MetricFlow, Malloy and KtX were re-run with pinned environments, so all 16 questions match
  across the five layers run on the current data. Each run records a fingerprint of the dataset
  it read; the Snowflake capture, which predates the current data, is reported as stale rather
  than as a mismatch.
