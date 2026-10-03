# Canonical answers

Case IDs are stable and can be cited in pull requests and reviews. A number is
canonical only when a cited declaration decides it; without one, the case expects
a clarification. `answers.yml` is also readable by the package test runner.

To add a case, cite the measure or dimension and the decision that governs it.
Write portable reference SQL from that rule, then run
`uv run python -m tests.integration.correctness.answer_ledger show <id>`.
Paste the printed `expected_rows` and check them against `shop/data/seed.sql`'s
comments. SQL columns follow the frozen row's key order. `make answers` checks
engine answers, independent references, citations, fixture data and planner intents.
Postgres checks run when the existing backend's environment is configured; CI
runs both DuckDB and Postgres.

A frozen row changes only with the declaration it cites, or with a `why` naming
a defect. Cite Markdown heading slugs, YAML dotted key paths, or Python top-level
names as `repo-relative-file#anchor`. Tags use the closed list in `answer_ledger.py`.
An explicit `order_by` requires row order; otherwise comparisons preserve the
multiset, including duplicates. Decimal comparisons retain exact values.

`known_wrong` allows only `engine` and `planner`, each with a nonempty reason.
It marks only that check as a strict expected failure; reference SQL and hygiene
always pass. Remove the marker when its fix makes the check pass.

After an intentional seed change, run
`uv run python -m tests.integration.correctness.answer_ledger fingerprint`
and update `fixture.data_sha256`. Only the listed tables contribute, so adding an
unrelated table does not change the fingerprint.
