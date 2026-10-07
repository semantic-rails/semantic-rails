- A request naming an object hidden from the caller by an `object_visibility` policy (`hidden`
  or `visible_only`) has exactly the outcome it would if the object did not exist, on every MCP tool and
  HTTP operation: the same envelope, code, message, details and suggestions as an id that names nothing
  (`OBJECT_NOT_FOUND`, `INVALID_TEMPORAL_ROLE` or `PATH_NOT_FOUND`), instead of
  `POLICY_DENIED`. `POLICY_DENIED` remains for objects the caller can see (`deny`, `redact`,
  `withhold_values`, metric constraints) and for a hidden object read only through a visible
  one. Refusals and `policy_effects` no longer name an object hidden from the caller. Effects
  referencing any hidden object are omitted in full, including their free text and policy ids.
