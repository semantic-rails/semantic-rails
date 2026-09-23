- MCP `segment-validate`, `segment-explain` and `segment-preview` honor the MCP minimal default,
  as `validate`, `compile` and `execute` already did: each returns what it is for (validity and
  the derived query; the definition, derived query and SQL; member rows and counts) without the
  compiler plans. Pass `verbosity="full"` for the whole response; HTTP is unchanged.
