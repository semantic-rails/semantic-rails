- Store grouping now uses the requested ID, name or label, so distinct stores
  sharing a name remain separate when grouped by ID. Ambiguous attributes require
  clarification, and asking for a total store count no longer adds grouping.
  Bare "by store" retains name grouping. Store grouping also recognizes tabs,
  newlines, long requests and "at the store dimension", "level" or "grain" clauses;
  a store filter cannot replace a requested grouping.
