- Count or sum parents with matching children without multiplying their values,
  including paths through a lookup or an alternate join key when the child
  route is the only candidate or is pinned by the package author. Ambiguous child
  groupings and negations remain refused, and under a row policy these queries
  are refused, as before.
- ClickHouse retains parent deduplication for key-based descents, including beside
  lookup selections, groupings or filters. It refuses lookup-before-child paths
  and paths joined off the parent's declared key, including beside a lookup.
