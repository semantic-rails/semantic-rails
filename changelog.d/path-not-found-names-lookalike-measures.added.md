- A call refused with `PATH_NOT_FOUND` because its measure's model can't reach a grouped or
  filtered dimension now names the same measure on another model that answers the call as it
  stands, when one does: `details.lookalike_measures` (each with the model's description of its
  rows) and a `use_lookalike_measure` hint. Each is checked by compiling the call with it, under
  the caller's own visibility and policies; a measure the caller can't see or read is never
  named, and none is chosen for the caller. See the `PATH_NOT_FOUND` row in
  [docs/MCP_INTERFACE.md](docs/MCP_INTERFACE.md).
- A measure's `inspect` card at `verbosity="full"` lists `grouping_entities`: the entities
  whose dimensions a call reading it can group by through a declared route.
