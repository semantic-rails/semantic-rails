- A call nested more than 250 levels deep is refused at the field where it is deepest
  (`INVALID_QUERY`, with `details.depth` and `details.max_depth`) before any work, instead of
  failing with an internal error. 250 levels is what the compiler fits in Python's default
  stack with room for the program calling it: copying a compiled plan, and turning it into the
  answer's fields, no longer take a frame of the stack for each level, and neither do the
  checks of a timed call's clocks, so a monthly call that deep answers on Python 3.11 as well.
  A division nested in its
  divisor 100 times, which failed with an internal error, now compiles. See
  [Refusals](docs/QUERY_CALL.md#refusals).
- A boolean `and` or `or` with hundreds of arguments compiles: from about 250 arguments it
  failed with an internal error, and from about 1,000 with `INVALID_CONFIG`. Up to 16
  arguments its SQL reads as before; a longer one groups its halves in parentheses.
- A `where` that lists thousands of values for one dimension answers in about a second: the
  check that warns about a value no row has ran one warehouse query per value, so 10,000
  values took about 30 seconds. It now reads them in one query, and its warnings are unchanged,
  however few rows the answer itself may return.
