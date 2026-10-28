- A share's denominator is held to the caller's policies as a call of its own, grouped as the
  share takes it (without the `over` columns and the key beside each `over` name): under a
  policy that requires a grouping, a share over that grouping is refused with `POLICY_DENIED`,
  naming the grouping and the share, where it used to return the total the policy refuses (the
  value divided by its share). A share within the required grouping, over the period or another
  column, still answers. A call with an entity filter or `routes` is checked as before; see
  [Known limitations](docs/QUERY_CALL.md#shares-and-totals-under-a-required-grouping).
- An expression `kind` padded with spaces (`" total "`) now reads as that kind everywhere, so a
  total or share written that way is held to the caller's policies as it is unpadded.
