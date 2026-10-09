- Entity-only metric predicates aligned to a query window now refuse ambiguous
  input clocks with `INVALID_TEMPORAL_BINDING` and candidate clocks to choose from.
  Measure defaults and bindings inside a metric advertising several clocks do not
  resolve the ambiguity; recovery guidance distinguishes direct measure and metric inputs.
- A window-aligned metric predicate refuses with `INVALID_TEMPORAL_BINDING` if the
  chosen window clock is excluded by any measure inside the input, by its pin, an
  override or its declared clocks; for a conversion, its base measure (the period
  filters base events; converted events match each base event's window).
