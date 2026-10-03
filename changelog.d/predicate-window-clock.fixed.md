- Entity-only metric predicates aligned to a query window now refuse ambiguous
  input clocks with `INVALID_TEMPORAL_BINDING` and candidate clocks to choose from.
  Measure defaults and bindings inside a metric advertising several clocks do not
  resolve the ambiguity; recovery guidance distinguishes direct measure and metric inputs.
