- MCP `inspect(verbosity="minimal")` states each fact once: it leaves out fields that
  repeat another field (`object_type`, `usage_summary`, `top_values`), empty structural fields and all
  but the first starter patch. Declared values and query literals stay exact, including blank/null.
  Omitted verbosity, `"compact"` and `"full"` return the whole v1 card on MCP and HTTP.
  Explicit HTTP `verbosity="minimal"` uses the same slim card projection as MCP.
