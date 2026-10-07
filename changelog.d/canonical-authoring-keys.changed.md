- Package authoring accepts only `relation`, dimension `kind`, time `class`,
  metric `temporal_role`, segment `basis_metric`, and nested `accumulation.snapshot`.
  `project upgrade` rewrites their legacy aliases; conflicting spellings require
  a manual edit. Retired keys report generic unknown-key errors.
