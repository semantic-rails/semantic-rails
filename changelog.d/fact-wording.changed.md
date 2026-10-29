- Deterministic notes now say exactly what they checked. `REWRITE_APPLIED` for measures of
  different models reads "Each model's measures were aggregated from its own rows to the call's
  groups before the join, so no row is counted twice. It does not check that these models
  answer the question." (`details.rewrite_classification`: `independent_leaf_alignment`). A
  row-grain model's `key_note` on `inspect` says no model can join to one of its rows by key,
  and what its measures still group and filter by. `TOTAL_OVER` names what a total rolls up
  and the grain it keeps ("with period rolled up: one value per store, repeated on each of its
  rows"), and the call schema describes a total's `over` the same way. The `execute` tool
  description and the HTTP docs say `ok` means the call is valid and ran, not that it answers
  the question.
