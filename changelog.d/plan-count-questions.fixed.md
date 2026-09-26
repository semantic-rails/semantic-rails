- `plan` answers "number of customers" and "how many customers" with Customer count. The
  words "number" and "of" tied it with measures described as "Number of …", and the tie went
  to Active menu count by label; a measure named for what the question counts, plus "count",
  now counts as the one the question names.
- The query MCP `discover` schema no longer advertises a `limit` default, so a client that
  fills in schema defaults gets 100-id pages for empty `terms`, not 10. Two recovery hints that
  pointed MCP agents at HTTP routes now name `discover`.
