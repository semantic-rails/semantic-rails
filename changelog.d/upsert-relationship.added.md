- The Architect MCP's `upsert_relationship` relates two entities through key
  columns in one transaction (with dry run, revision and idempotency checks):
  the foreign key goes in the model's `entities:` block, and a one-to-one
  cardinality, a name, allowed directions, safety, a path preference, a label
  or a description also write `graph.relationships`. One-to-many is recorded
  from the many side; many-to-many is refused with bridge-model guidance.
