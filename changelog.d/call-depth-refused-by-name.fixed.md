- A call nested more than 100 levels deep is refused at the field where it is deepest
  (`INVALID_QUERY`, with `details.depth` and `details.max_depth`) before any work, instead of
  failing with an internal error after seconds. 100 levels is what the compiler fits in
  Python's default stack with room for the program calling it. See
  [Refusals](docs/QUERY_CALL.md#refusals).
- A boolean `and` or `or` with hundreds of arguments compiles: from about 250 arguments it
  failed with an internal error, and from about 1,000 with `INVALID_CONFIG`. Up to 16
  arguments its SQL reads as before; a longer one groups its halves in parentheses.
