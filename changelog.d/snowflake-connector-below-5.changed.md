- The `snowflake` and `all` extras require `snowflake-connector-python` below 5. Connector 5.x
  drops the certificate revocation settings that Snowflake connections pass, so a fresh install
  stays on the 4.x connector until 5.x support is verified.
