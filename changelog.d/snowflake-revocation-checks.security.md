- `snowflake_native` connections check Snowflake's TLS certificates for
  revocation again. Snowflake connector 4.8 stopped checking unless asked;
  connections now use the connector's revocation-list check in advisory mode,
  which refuses a revoked certificate without failing when the list cannot be
  fetched. Set `cert_revocation_check_mode` to `enabled` or `disabled` in
  `package.connection.options` to change it; see
  [Snowflake certificate revocation checks](docs/PACKAGE_AUTHORING.md#snowflake-certificate-revocation-checks).
