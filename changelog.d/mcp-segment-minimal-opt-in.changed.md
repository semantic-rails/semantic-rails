- MCP `segment-validate`, `segment-explain` and `segment-preview` accept `verbosity="minimal"`
  to return what each tool is for (validity and
  the derived query; the definition, derived query and SQL; member rows and counts) without the
  compiler plans while retaining query/segment policy effects and actionable recovery hints.
  Omitted verbosity and `"full"` keep the v1 whole response; HTTP is unchanged.
