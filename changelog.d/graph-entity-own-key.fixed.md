- Refuse graph entities without a key instead of borrowing another exposed
  entity's key. Graph model bindings determine the primary entity independently
  of declaration order, and invalid or unattached graph relationships now fail
  loading with a named `INVALID_CONFIG` error instead of being silently omitted.
