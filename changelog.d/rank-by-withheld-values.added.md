- A new `object_access` action, `withhold_values`, lets a caller rank by a metric or
  measure without seeing its values: "the 3 biggest accounts in EMEA by revenue" returns
  the accounts only, with a top-level `withheld` list and a `VALUES_WITHHELD` warning. The
  query selects the metric directly and orders by it first, then by every group key in the
  same direction (added when omitted), with a `limit` of at most `config.max_rank`
  (default 10, at most 100). Every other use (a selected expression or derived metric that
  reads it, a filter or threshold on it, a segment on it, `export`, a larger limit) is
  refused with `POLICY_DENIED` and `details.withheld_objects`. `deny` and `redact` are
  unchanged.
