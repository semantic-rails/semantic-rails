- `semantic-rails mcp start`, `status` and `stop` no longer hang when `ps` does not
  answer. The process check gives up after five seconds and treats the server as
  unverified, so `stop` never signals a process it could not identify. If identity
  observation fails, `stop` reports `identity_unverifiable` and keeps the server
  registered so the stop can be retried.
