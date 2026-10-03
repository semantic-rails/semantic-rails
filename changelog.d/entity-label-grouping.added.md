- Packages can declare an entity's `label_dimension` so plans grouping by the
  entity or its own key dimensions include every key component before the label,
  without merging entities that share a name. Explicit
  label-dimension grouping remains label-only; foreign-key dimensions on other
  entities do not expand.
  Loading refuses labels or key components without groupable dimensions. Entity
  inspect cards include visible declared labels and explain undeclared labels for
  non-time entities.
