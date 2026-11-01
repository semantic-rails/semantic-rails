- `discover` lists a canonical model's first row ahead of the rows of the look-alike models its
  `prefer_over:` lists, and otherwise keeps its match order, so a weak match from the canonical
  model never moves ahead of a better match from another model. It marks each look-alike's card
  with `look_alike`: `history copy of <entity>` when its key is the canonical model's plus time
  columns of its own, else `prefer <entity>`. Slim cards mark a look-alike's measures, metrics,
  segments and entity; full cards mark every one. A package without `prefer_over:` gets the
  same response; a caller who can't see either model gets neither; `plan` and `execute` are
  unchanged. See [MCP_INTERFACE.md](docs/MCP_INTERFACE.md).
