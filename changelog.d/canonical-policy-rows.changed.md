- Policy rows accept only flat, kind-specific keys and explicit `action` and
  `rationale` fields. Unknown keys and aliases refuse with `INVALID_CONFIG`;
  `semantic-rails project upgrade` provides `policy-flat` and `policy-redact-deny`
  previews; refused legacy forms are currently `unverified` and cannot be written.
  Nested scope fields and disagreeing alias values stop without choices.
- `redact` never masked values; it refused like `deny`. Refusals and inspect now
  name `deny`. Packages and Python-built policies that still use `redact` refuse.
