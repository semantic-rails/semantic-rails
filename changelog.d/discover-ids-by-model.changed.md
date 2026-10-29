- `discover` with empty `terms` lists the ids to start from by model: each model the caller sees,
  with its measure, metric, segment and dimension ids, under `counts` and the package
  `description`. When the caller sees more than 100 dimensions, each model gives its
  `dimension_count` (the note `DISCOVER_DIMENSIONS_COUNTED`) and `kinds: ["dimension"]` pages
  their ids. `limit` and `offset` page the models, and a page ends at the models that fit the
  response budget, so the default call on a large package no longer fails with
  `RESULT_TOO_LARGE`. With `kinds`, or under a resource grant, the listing is by kind as before.
  The listing no longer repeats the package's settings and capability flags (read them from the
  `semantic-rails://catalog/index` resource), and leaves out relationship, temporal role and
  value domain ids unless `kinds` names them. The jaffle_shop default listing is 6,475
  characters, from 9,393.
- The `discover` tool's `limit` argument says that `has_more` counts what a list left out.
