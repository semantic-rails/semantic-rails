- Update the locked urllib3 and PyJWT dependencies to patched releases for published
  security advisories. The Databricks connector still requires oauthlib below 4.0,
  leaving CVE-2026-49265 unresolved for the `databricks` and `all` extras. Track this
  connector-only exception with a 30-day review limit and automatic rejection
  when the connector's latest PyPI dependency metadata permits any advisory-reported
  patched version, including backports. Audit every published extra and require
  `all` to equal their union; core advisories remain a hard gate.
