- `semantic-rails project upgrade` rewrites example and test queries written in the previous
  call shape as the current call (rule `query-call-v2`): reference objects, `metric_filters`,
  `route_decisions` and route options, `policy_context` (now `now:` and `context:` on the entry),
  `time.range`, equivalent SQL ops, `div`/`sub`/`mul`, `operands`/`terms`
  and tool arguments sent inside the query, with the time column renamed `period` in the entry's
  expected columns and rows. Both `metric_query` and `expected_query` in a comparison test
  are included in the rewrite and proof. A `validate_fails_with_code` test is listed as a
  hand edit with a suggested call and its expected code to verify. Moving a comparison's
  clock or context to its shared entry also requires a hand edit. Answer-changing translations
  (`!=` or `NOT IN` keeps unknown rows; a text word replacing `LIKE` ignores case) and sites
  with no current form stop the upgrade at their file, line and path. Nothing is written
  until the hand edits are resolved. Edits keep the lines and comments they don't change.
