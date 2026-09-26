- The query MCP costs a model less context. Tool descriptions and schema prose are shorter,
  with the same tools, arguments, enums and defaults; responses leave out empty optional fields
  and repeats of an error's code or message; and default (`minimal`) `discover` cards leave out
  their bucket's `kind`, `available: true` and empty fields.
- The query MCP's `discover` accepts `kinds` sent as a JSON array inside a string
  (`'["measure", "metric"]'`), as some models send it; it used to match no kind, so every bucket
  came back empty with a `DISCOVER_UNKNOWN_KIND` warning. A select item written without its
  `expression` wrapper now gets an error that names the shape to use.
