- Package authoring accepts only `relation`, dimension `kind`, time `class`,
  metric `temporal_role`, segment `basis_metric`, and nested `accumulation.snapshot`.
  `project upgrade` rewrites their legacy aliases; conflicting spellings require
  a manual edit. Retired keys report generic unknown-key errors.
  An unknown key in a model, dimension, time, metric, segment or
  `defaults.dimension|time|measure` now fails loading instead of being only a
  `validate` finding.
