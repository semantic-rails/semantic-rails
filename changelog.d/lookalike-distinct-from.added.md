- A model's `distinct_from:` lists, by model id, models whose rows are different things though
  validation groups them as look-alikes, such as stores and their daily inventory snapshots.
  Either model listing the other takes the pair out of its `LOOKALIKE_MODELS` group, of either
  kind; `discover` marks neither as the other's look-alike, and a `PATH_NOT_FOUND` refusal or a
  measure's card never names a measure of one as the other's look-alike. An unknown model id,
  and a pair in both a `prefer_over:` and a `distinct_from:`, are `INVALID_CONFIG`. A package
  that declares none keeps its semantic fingerprint. See
  [PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md#distinct_from--models-that-only-look-alike).
