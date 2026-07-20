# OSS Release Checklist

Repository code enforces the build, semantic, packaging, dependency, and public-boundary gates. The
remaining controls live in GitHub or PyPI and must be confirmed by a repository administrator.

## One-time repository settings

- Protect `main`; require pull requests and the `All checks pass` status, and block force pushes
  and branch deletion.
- Enable secret scanning, push protection, private vulnerability reporting, and Dependabot
  security updates.
- Require organization-wide two-factor authentication after confirming every current member is
  enrolled; enabling it removes members who have not enrolled.
- Keep the `pypi` environment and its Trusted Publisher limited to
  `.github/workflows/publish.yml`; add required reviewers if releases need a
  manual approval point.
- Set the repository description, homepage (`https://semantic-rails.com`), and topics.

## Every release

- Make the release PR green, including Python, lint/type, Python and npm security audits, docs,
  the planner benchmark, clean-wheel installation, and package tests.
- Run `python scripts/generate_contract_artifacts.py --check` and
  `python scripts/check_contract_compatibility.py --baseline <previous-release-contracts>`.
  Classify contract changes as none/additive/breaking and require a contract-owner review.
- Confirm `architect_mcp.v1.json` drift tests and the Architect transaction
  suite are green, including stale-writer conflict, cross-process serialization,
  idempotent replay, write-free preview, and raw-write rollback cases.
- Build the release-candidate wheel once. Run the public dbt and SQLMesh
  validation-binding suites against that exact wheel and contract bundle before publishing.
- Run `make release-check` against the release candidate.
- Tag exactly `v<project.version>`. The publish workflow rejects a tag/version mismatch.
- Approve the PyPI environment only after the release gate succeeds. The publish workflow builds
  wheel and sdist once, publishes those verified bytes, matches both PyPI digests, and repeats one
  governed `plan → validate → compile → query` smoke from the public index.
- Confirm the post-publish byte verification is green. Only then may the
  workflow create the matching GitHub Release and attach the exact wheel,
  sdist, every public contract JSON artifact, and `SHA256SUMS`.
- Download the GitHub Release assets, run `sha256sum --check SHA256SUMS`, and
  confirm their wheel/sdist digests match PyPI. No deployment workflow is
  coupled to this repository release.
