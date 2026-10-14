- A `row_filter` policy reads a list attribute as any of its values: a trusted attribute
  such as `regions: ["West", "East"]` filters with `IN (?, ?)`, one bound parameter per
  item, and an empty list denies the request with `POLICY_DENIED` instead of raising a
  `TypeError` when the host builds the context. A new `unless_roles:` key names the roles
  a row filter does not apply to, and `validate` warns about a row filter scoped by
  `roles:`, which leaves every other role unfiltered. See
  [docs/PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md).
