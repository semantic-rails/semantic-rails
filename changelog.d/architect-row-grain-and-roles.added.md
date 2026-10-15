- Architect MCP `upsert_relationship` relates a row-grain model from its side (by its model id or
  its entity id), names either entity by its key or its id, and with `name` writes a second role
  on an entity pair as its own `graph.relationships` entry; a relationship into a row-grain model
  is refused with the call that writes it from that model's side. `upsert_model` takes
  `entities`, the model's parent entities. [Two roles on one pair](docs/PACKAGE_AUTHORING.md#two-roles-on-one-pair)
  shows a worked example.
