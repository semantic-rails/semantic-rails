- Missing review metadata (`meta.owner_team`, `meta.review_priority`, `meta.change_risk`) is
  reported only by the promotion check, as one `REVIEW_METADATA_MISSING` advisory per measure or
  metric; parse and validate leave it out, so a new package's first warnings are the ones that
  change answers.
- Parse and validate messages name files relative to the current directory when the package
  lies under it (`semantic_rails/jaffle/models/customers.yml` from a dbt project root), else
  relative to the package directory, instead of repeating its absolute path.
- Every Architect MCP write starts with `outcome`: three lines saying what happened, the
  package's state after it, and what to do next, with the whole revision to send. A summary shows repeated warnings only while
  they fit 2,000 characters, after the first warning of each code.
