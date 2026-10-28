- `discover` accepts every kind its tool schema lists. `temporal_role` and `value_domain` (or
  their plurals) with `terms` used to be refused with "valid kinds: …" although the schema
  offered them; they are now ranked when named, in `temporal_roles` cards (the `dimension` each
  role times and its `supported_grains`) and `value_domains` cards (the `dimensions` each
  describes). With empty `terms`, `dimension_value` lists `dimension_value_ids`.
- `plan` with a one-item seed, such as `{"select": ["metric.<id>"]}` over a measure of the same
  name, no longer fails with `DUPLICATE_OUTPUT_ALIAS` "('' is taken)": the seed stands for that
  measure, and a question whose pieces don't connect gets `PLAN_UNCONNECTED` naming them, as it
  does without the seed.
- Several `dimension.` ids in a call's `select` move to `group_by` the way one does, with one
  `QUERY_SHORTHAND_NORMALIZED` notice listing them all. The second one used to be refused with
  "a dimension splits the answer; it belongs in group_by".
