- MCP `discover` cards at the default and `compact` verbosity leave out a `label` whose every
  word is already in the card's id ("Store name" on `dimension.store_name`), so a ranked
  response is about 15% smaller on the bundled package. A label that adds a word ("Orders"
  on `measure.order_count`), blocked cards, dimension values and `verbosity="full"` keep it.
  See [MCP interface](docs/MCP_INTERFACE.md).
