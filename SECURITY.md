# Security Policy

## Reporting A Vulnerability

Please do not open a public issue for a suspected vulnerability.

Report security concerns privately through GitHub's private vulnerability reporting for this
repository:
[https://github.com/semantic-rails/semantic-rails/security/advisories/new](https://github.com/semantic-rails/semantic-rails/security/advisories/new)
("Security" tab → "Report a vulnerability").

Include:

- A short description of the issue and affected surface.
- Steps to reproduce, if available.
- Any relevant logs, request payloads, or package snippets.

The maintainer will acknowledge valid reports as capacity allows, investigate, and coordinate a
fix or disclosure path when the issue affects supported code.

## Supported Surface

Security reports should focus on the active runtime, package loading, API server, MCP adapters,
documentation, and demo surfaces shipped in this repository. Experimental notes and archived
planning documents are not supported runtime guarantees.

## Trust Boundaries In The OSS Default

The bundled `HeaderPolicyContextResolver` reads `roles`, `audience`, `environment`, and `tenant`
from request headers and the request body's `policy_context`. This is safe for local
single-tenant use where the caller is the operator. It is **not** safe in multi-tenant hosted
deployments — a caller can self-assert any role.

Two consequences for the OSS default:

- **Raw SQL is never returned in error envelopes unless three conditions all hold:**
  the request sets `debug: true`, the operator sets the env var
  `SEMANTIC_RAILS_ALLOW_DEBUG_SQL=1`, and the resolved request context carries the `debug`
  role. The env var exists so that role-based exposure is impossible by default — operators
  flip it only after replacing the resolver with one bound to authenticated identity.
- **Hosted deployments MUST replace the resolver** via
  `semantic_rails.request_context.set_policy_context_resolver(...)` with a resolver that
  derives those fields from authenticated identity (JWT, mTLS, signed session). The
  swap is the integration seam; the policy engine and Query IR stay untouched.

## Probe SQL Surface

Authored `table:` identifiers from package YAML flow into the column-reachability probe
(`semantic-rails check`).
Each dotted part is validated against a strict SQL-identifier regex
before any DESCRIBE or `information_schema.columns` lookup runs, and Snowflake probes use
parameter binds. An adversarial package cannot mutate the probe SQL by authoring a malicious
identifier; the check returns a structured `WAREHOUSE_TABLE_NOT_FOUND` error instead.
