- MCP `inspect(verbosity="minimal")` states each fact once: it leaves out fields that
  repeat another field (`object_type`, `usage_summary`, `top_values`), empty fields and all but the
  first starter patch. Omitted verbosity, `"compact"` and `"full"` return the whole v1 card; HTTP
  `inspect` is unchanged.
