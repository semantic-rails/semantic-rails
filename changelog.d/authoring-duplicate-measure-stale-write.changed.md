- Package validation warns when a measure duplicates another one (same entity, expression,
  aggregation and default clock).
- An Architect write refused as stale now says that writes sent together with one
  `expected_revision` apply only the first, and names the revision to resend with; the
  Architect MCP instructions ask for one write at a time.
