- MCP `inspect` states each fact once by default: `verbosity="minimal"` leaves out fields that
  repeat another field (`object_type`, `usage_summary`, `top_values`), empty fields and all but the
  first starter patch. `verbosity="compact"` or `"full"` returns the whole card as before; HTTP
  `inspect` is unchanged.
