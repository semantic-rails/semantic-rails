- `object_visibility` no longer accepts `action: visible`, which had no effect; a package
  using it fails to load with `INVALID_CONFIG` naming `visible_only`. A policy or caveat whose
  `environments:` names an environment that `package.environments` doesn't declare, or that
  is scoped in a package declaring none, also fails to load with `INVALID_CONFIG` instead of
  never applying.
- `plan` refuses a draft that reads any hidden object, not only a hidden dimension, with
  `OBJECT_NOT_FOUND`, and no longer offers a hidden measure as an Intent IR subject. An
  `inspect` card no longer names a hidden object among its related measures and metrics,
  comparison peers, clock variants, companions or default metric.
