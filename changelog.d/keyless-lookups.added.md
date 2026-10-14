- `one_per_key: true` on a model reads its relation as one row per its entity's key, so a table
  whose rows repeat a key can be looked up: each column is the key's one value, and NULL where
  its rows hold two. A row-grain model over the same relation, with a foreign key on that key,
  is its link: grouping by the link's columns counts each entity once in every group it has a
  row in (sums, counts, averages and distinct counts), a row with no value there is the NULL
  group, and filters keep the entities with a matching row. Shares and windows across a link
  are refused. See [One row per key](docs/PACKAGE_AUTHORING.md#one-row-per-key) and
  [Groups across a link](docs/QUERY_CALL.md#groups-across-a-link). `upsert_model` takes
  `one_per_key`.
