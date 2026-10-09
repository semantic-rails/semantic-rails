- Policy rows accept only flat, kind-specific keys and explicit `action` and
  `rationale` fields. Unknown keys and aliases refuse with `INVALID_CONFIG`;
  `semantic-rails project upgrade` rewrites both with the certified rules `policy-flat` and
  `policy-redact-deny`, in the same write as the package's other legacy forms. Nested scope
  fields and disagreeing alias values stop without choices.
- `redact` never masked values; it refused like `deny`. Refusals and inspect now
  name `deny`. Packages and Python-built policies that still use `redact` refuse.
