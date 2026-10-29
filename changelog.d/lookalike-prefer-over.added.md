- A model's `prefer_over:` lists the look-alike models, by model id, whose questions it answers
  instead. A group of look-alike models is decided once one model lists every other, directly or
  through another model's list, and its `LOOKALIKE_MODELS` warning clears. An unknown id, the
  model itself, or lists that run in a circle are `INVALID_CONFIG`. The Architect's
  `upsert_model` writes it (`prefer_over`; `[]` removes it, and `replace` keeps it). The warning
  now names models by model id, with their entities in `details.entities`. See
  [`prefer_over:`](docs/PACKAGE_AUTHORING.md#prefer_over--the-canonical-model-of-look-alikes).
