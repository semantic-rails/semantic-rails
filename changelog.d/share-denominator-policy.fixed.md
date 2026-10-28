- A share's denominator is held to the caller's policies as a call of its own, grouped as the
  share takes it (without the `over` columns and the key beside each `over` name): under a
  policy that requires a grouping, a share over that grouping is refused with `POLICY_DENIED`,
  naming the grouping and the share, where it used to return the total the policy refuses (the
  value divided by its share). A share within the required grouping, over the period or another
  column, still answers, and an entity filter that reads each entity's own aggregate doesn't
  refuse a share of another input. See [Share of a total](docs/QUERY_CALL.md#share-of-a-total).
- A total or a share in a call with `routes` is held to the caller's policies too: a route the
  call without the `over` columns no longer walks used to skip the check, and the total came
  back. An expression `kind` padded with spaces (`" total "`) now reads as that kind everywhere.
