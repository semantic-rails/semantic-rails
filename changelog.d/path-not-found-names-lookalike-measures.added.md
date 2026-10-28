- A call refused with `PATH_NOT_FOUND` because its measure's model can't reach a grouped or
  filtered dimension now names the same measure on another model that answers the call as it
  stands, when one does: `details.lookalike_measures` (each once, with the first sentence of its
  model's description and the result columns to rename) and a `use_lookalike_measure` hint. A
  look-alike has the same name and the same column expression; a table's row count never has
  one. Each is checked by compiling the call with it, under
  the caller's own visibility and policies; a measure the caller can't see or read is never
  named, and none is chosen for the caller. See the `PATH_NOT_FOUND` row in
  [docs/MCP_INTERFACE.md](docs/MCP_INTERFACE.md).
- A measure's `inspect` card names its look-alikes on other models, when it has any:
  `lookalike_measures`, each by id with the first sentence of its model's description and
  which of the seed call's dimensions it reaches, with the card's own `grouping_entities`,
  `model_description` and a fixed `lookalike_notice`. Only models the caller sees and may read
  are named; cards without look-alikes are unchanged.
