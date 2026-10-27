- The comparison pack's Semantic Rails evidence is recaptured on 0.3.2 (`main` after the
  release), and its latest-release check now runs PyPI `semantic-rails==0.3.2`, which answers
  all 8 frozen-model questions. q11 and q12 now compute each customer's lifetime order count and
  spend from orders with a query-time customer predicate (`over: all_time`) instead of reading
  the precomputed customer columns, so the rubric labels all 24 Semantic Rails answers
  `native`; every answer still matches the independent answer key.
