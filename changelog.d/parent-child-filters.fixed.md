- Count or sum parents with matching children without multiplying their values,
  including paths through a lookup or an alternate join key. Ambiguous child
  groupings and negations remain refused, and under a row policy these queries
  are refused, as before.
- ClickHouse retains parent deduplication for supported child filters and refuses
  combinations with lookup selections, groupings or filters, lookup-before-child
  paths and paths joined off the parent's declared key.
