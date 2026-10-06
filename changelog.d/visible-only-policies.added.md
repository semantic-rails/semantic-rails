- `object_visibility` policies take `action: visible_only` with `object_ids` and `roles`
  and/or `audiences`: the objects, and everything computed from them, are hidden from every
  request the policy doesn't name (including one with no roles) in catalog, discovery,
  inspect, valid values, planning and diagnostics, and any query reading them is refused
  with `POLICY_DENIED`. Several policies on one object must all be met, and `hidden`, `deny`,
  `redact` and `withhold_values` still apply to the named roles. See
  [Objects visible only to named roles](docs/PACKAGE_AUTHORING.md#objects-visible-only-to-named-roles).
  Full catalog payloads filter restricted companions, requests naming environments the package
  does not declare are refused, and callers with restrictions cannot conditionally aggregate raw columns.
