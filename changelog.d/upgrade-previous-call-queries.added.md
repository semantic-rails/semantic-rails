- `semantic-rails project upgrade` rewrites example and test queries written in the previous
  call shape as the current call (rule `query-call-v2`): reference objects, `metric_filters`,
  `route_decisions` and route options, `policy_context` (now `now:` and `context:` on the entry),
  `time.range`, SQL ops, `LIKE` patterns a text word says, `div`/`sub`/`mul`, `operands`/`terms`
  and tool arguments sent inside the query, with the time column renamed `period` in the entry's
  expected columns and rows. Each hit names the sites whose answer can move (`not` keeps unknown
  rows; a text word ignores case). A site with no current form stops the upgrade at its file,
  line and path, and a `validate_fails_with_code` test keeps the query it expects refused. Edits
  keep the lines and comments they don't change.
