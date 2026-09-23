- Query patches from `discover`, `inspect` and `build-options` contain only Query IR fields. They
  no longer copy the caller's `policy_context` or the tool's other arguments, so each patch
  validates as returned.
