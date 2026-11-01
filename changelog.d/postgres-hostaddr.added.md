- **Postgres pinned address:** `postgres_native` connections accept `hostaddr`, one IP address
  of the server `host` names. The connection dials it on every connect without looking `host`
  up, while TLS still sends `host` as the server name and `sslmode: verify-full` checks the
  certificate against it ([ADBC profiles](docs/ADDING_A_DIALECT.md#adbc-profiles)).
