- SQL lowering refuses stock measures routed through row-based fan-out or parent-lookup
  rewrites, including supplied plans that bypass earlier validation, so attribute filters
  cannot bypass snapshot selection.
