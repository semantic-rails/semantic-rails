- A call grouped by a child's columns can select a measure of the parent each child row names
  (each order with its customer's lifetime order count, each room with its building's area)
  instead of being refused with `PATH_NOT_FOUND` or asked `AMBIGUOUS_PATH` from the parent's
  side. It is read on the children's rows by the route the call or package records from them,
  else their own key: each parent counts once, with its whole value, in every group that holds
  one of its child rows, a group whose rows name no parent reads NULL, and a total is the call
  without the grouping. `REWRITE_APPLIED` (`parent_value`) says the column doesn't add up across
  groups. A route recorded from the parent's side stands, and a median, a non-additive measure,
  a ratio against the child's measure, a call with `time`, `having` or no `group_by`, and any
  such call on ClickHouse (which joins lookups inner) keep their refusal. See
  [A parent's value on its children's rows](docs/QUERY_CALL.md#a-parents-value-on-its-childrens-rows).
