- Refuse graph entities without a key instead of borrowing another exposed
  entity's key. Graph model bindings determine the primary entity independently
  of declaration order, and invalid or unattached graph relationships now fail
  loading with a named `INVALID_CONFIG` error instead of being silently omitted.
- Remove the declaration-order fallback for primary entities and refuse graph
  relationships authored with `from`/`to`; use `entities: [source, target]` instead.
