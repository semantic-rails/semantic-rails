- Update the locked urllib3 and PyJWT dependencies to patched releases for published
  security advisories. The Databricks connector still requires oauthlib below 4.0,
  leaving CVE-2026-49265 unresolved for the `databricks` and `all` extras. Track this
  connector-only exception with a 30-day review limit and automatic rejection
  when any advisory-reported patched version becomes resolvable, including backports.
  Audit every published extra; core advisories remain a hard gate.
