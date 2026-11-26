- A metric constraint's `allowed_where` now also covers the rows an `aggregate_if` value picks,
  not only its condition: a `case` in the value is read as a condition, and any other column
  the value computes with counts as the dimensions over it. A field the policy doesn't list is
  refused with `POLICY_DENIED` and the same `disallowed_where` violation
  (`"source": "inline_expression"`) as a condition on that field. A `case` on an allowed field
  and a column the value returns as it is still answer.
