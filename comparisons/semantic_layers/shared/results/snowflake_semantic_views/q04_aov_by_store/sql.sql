-- Baseline: average order value by store.
SELECT *
FROM SEMANTIC_VIEW(
  ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON
  DIMENSIONS stores.store_name
  METRICS aov_usd
)
ORDER BY aov_usd DESC;
