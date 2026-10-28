- Architect's `suggest_links` proposes relationships from a table to other tables' keys from their
  values, where `suggest_model` only named candidates. Each pair states how many of the column's
  distinct values (and rows) are keys of the target, with a few values not found, for example
  "23 of 25 distinct subject_id values (92.0%) are subjects.subject_id keys". Pairs at or over
  the stated threshold (`min_coverage`, default 95%) are proposed and the rest are listed with the
  same evidence, so a partial link is the author's call rather than a hidden cut. `columns` tries
  columns named like no key (an office location holding room keys). With `project_path` each pair
  carries draft `upsert_relationship` arguments (`one_to_one` when both sides are unique). It
  writes nothing: apply the drafts with `dry_run`, then `expected_revision`.
- The package authoring guide says what an explicit `safety: safe` on a `many_to_many` history
  states: its versions never overlap, and nothing checks that at query time.
