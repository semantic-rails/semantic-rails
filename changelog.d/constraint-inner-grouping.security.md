- A metric constraint now holds inside the call, not only in its own `group_by` and `where`. A
  `distribution` over an entity's values, an entity filter or scoped-aggregate predicate whose
  input is the measure, and a lookup measure read from it all group the measure by an entity's
  keys; that grouping must be in the measure's `allowed_group_by`, must keep its
  `required_group_by`, and must read it under its `required_where`, or the call is refused with
  `POLICY_DENIED` and the usual violation kind (`disallowed_group_by`,
  `missing_required_group_by`, `missing_required_where`), naming the measure, the grouping and
  the cuts the policy allows. With revenue limited to store cuts, the median of revenue per
  customer is refused: "measure.jaffle.revenue_usd can't be grouped by
  dimension.jaffle_customer_id, as the distribution over entity.jaffle_customer groups it; it
  can be grouped only by dimension.jaffle_store_name." The same holds for the key the answer
  carries beside a name that labels an entity's rows, for a conversion's converted events (read
  without the call's filter on the base side), for a share beside an entity filter or inside a
  package metric, and for a policy on a package metric's id. A package metric with such an
  entity filter is refused the same way for a caller the constraint applies to; add the
  entity's key to `allowed_group_by` where a per-entity statistic is meant to be read. Callers
  no constraint applies to get the same SQL as before.
