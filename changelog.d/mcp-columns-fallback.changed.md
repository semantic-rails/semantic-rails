- MCP `execute` returns rows that exceed the response budget as records but fit as columns in
  the columnar form, with the note `ROW_FORMAT_COLUMNS`, from its one warehouse run. It used to
  refuse them with `RESULT_TOO_LARGE` and a hint to run the call again with
  `row_format: "columns"`. This applies only when the call sends no `row_format`; a response
  that fits as records is unchanged. Read `row_format` before reading `rows`.
