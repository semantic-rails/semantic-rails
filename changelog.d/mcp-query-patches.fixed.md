- Query patches from `discover`, `inspect` and `build-options` contain only Query IR fields, at
  every `build-options` step. They no longer copy the caller's `policy_context` or the tool's
  other arguments, and each validates as returned: `build-options` value filters use `field`, a
  patch for a windowed metric carries its default time window, and a `percentile` option carries
  `p`. These tools read Query IR only from their `query` argument, not from top-level fields.
