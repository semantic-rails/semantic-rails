- A `kind: lookup` measure (`from:` a measure, `via:` a parent entity) carries the parent's
  all-time total onto each of its child rows, such as a coverage's premium on each claim. It is
  answered only where each output row holds one parent: coarser groupings are refused with
  `ROLLUP_UNSAFE`, a child of the child with `MIXED_GRAIN_INVALID`, and time on the lookup itself
  with `REWRITE_NOT_SUPPORTED`. Composite parent keys and conflicting recorded routes are
  refused at load; access policies also govern the lookup's direct relationship. Physical
  relation names are preserved even when they match a nested lookup CTE name.
  See "Lookup measures" in
  [docs/PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md).
