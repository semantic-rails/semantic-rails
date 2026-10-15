- MCP `execute` refuses a key of the call sent beside `query` (a top-level `limit`, say) with the
  hint to move it inside, instead of ignoring it with a warning; its description says `limit`
  goes inside the call and `max_rows` and `row_format` beside it, and to run a call directly
  rather than validate it first.
- `REWRITE_APPLIED` is a note (severity `info`), with the same disclosure, so a correct
  multi-fact answer no longer carries a warning.
