- Validation names each group of models an agent can read for one another, with a
  `LOOKALIKE_MODELS` warning asking which one is canonical: models whose rows are the same things
  (the same key, that key plus time columns of its own, such as a history, or keyless with one
  reading every column of the other), and models whose rows aren't, each with a published count
  of the same name over the same column. `parse-config`, `validate-config`, `check`,
  `validate_project` and `project_status` report it, and `project_status` adds a next action
  counting the groups. A table and the one that holds its key, a calendar, and two tables' row
  counts are never a group. See
  [look-alike models](docs/PACKAGE_AUTHORING.md#look-alike-models).
