- `query_matches_snapshot` package tests compare numbers by value, so a
  DECIMAL result with cents matches the number written in the test's YAML.
  `metric_equals_query` compares its two results the same way, and mismatch
  details show numbers in that one form (trailing zeros dropped).
