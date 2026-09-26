- The query MCP costs a model less context. The server instructions and tool descriptions are
  shorter, with the same tools, arguments, enums and defaults; responses leave out empty
  optional fields and repeats of an error's code or message; `discover` cards leave out their
  bucket's `kind` and `available: true`; and `discover` with `verbosity="compact"` now returns
  slim cards plus each card's root entity, up to three match reasons and its starter patch
  (`verbosity="full"` returns the whole cards). A select item written without its `expression`
  wrapper now gets an error that names the shape to use.
