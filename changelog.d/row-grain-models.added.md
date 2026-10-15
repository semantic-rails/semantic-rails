- A model that no graph entity is homed on (its `entities:` block lists only its parents) is a
  row-grain model: it has no key, and every stored row counts. Its measures group by its own,
  its parents' and their parents' columns, a parent can be filtered or ranked by its rows, its
  rows can be filtered by a parent's other children, and it can be a link table. A relationship
  into one is refused at load, and what needs to tell its rows apart (a segment, a predicate, a
  conversion, a stock) is refused with `ROW_GRAIN_UNSUPPORTED`, naming the fix. See
  [Row-grain models](docs/PACKAGE_AUTHORING.md#row-grain-models).
- `suggest_model` confirms an inferred key on the whole relation before suggesting it, under one
  60-second deadline checked between statements, and reports how the search ended in
  `key_search`; a search that ends past the deadline suggests no key. With no confirmed key it
  drafts a row-grain model; an empty table keeps only its declared primary key. `upsert_model`
  accepts `primary_key: []`.
- `plan` holds a ranking of an entity with no single key (a row-grain model or a composite
  key) with `PLAN_RANKING_NO_KEY` instead of drafting one total with a limit.
