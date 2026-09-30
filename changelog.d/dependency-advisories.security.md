- Update the locked urllib3 and PyJWT dependencies to patched releases for published
  security advisories. The Databricks connector still requires oauthlib below 4.0,
  leaving CVE-2026-49265 unresolved for the `databricks` and `all` extras.
