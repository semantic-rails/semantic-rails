- `semantic-rails mcp start`, `status` and `stop` no longer hang when `ps` does not
  answer. The process check gives up after five seconds and treats the server as
  unverified, so `stop` never signals a process it could not identify.
