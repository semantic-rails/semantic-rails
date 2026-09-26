- A rollup's measure column can declare what it holds per row with `holds:`
  (`sum`, `min`, `max` or `count_distinct`), so `min` and `max` queries can run on
  a rollup. A column declared `holds: count_distinct` also answers a distinct count
  of a key other than the row key at the rollup's own time grain, when every
  rollup dimension is grouped or pinned by an `=` filter. A column without
  `holds:` keeps its meaning: a sum for an `aggregate` measure or a distinct count
  for an `entity_count` measure, re-added with `SUM`. A dimension column
  pre-joined from another model declares the relationship `path:` it was built
  along, and routes only when the query joins along that same many-to-one path.
