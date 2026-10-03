# Canonical answers

Case IDs are stable and can be cited in pull requests and reviews. A number is
canonical only when a cited declaration decides it; without one, the case expects
a clarification. Cases with a `variant` hold only under `test_answers.py`;
the product test runner is not a supported entry point for `answers.yml`.

Definitions are shop YAML anchors exactly at `model.measures.<name>`, `model.dimensions.<name>` or `model.times.<name>`; decisions are the doc anchors in `answer_ledger.py`'s `DECISIONS` set.

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
The harness reads frozen decimal literals as `Decimal`, preserving all digits
before result encoding; query parsing still uses the product package-test loader.

Every case built by `test_correctness.py` also has frozen rows here, with the same
query, variant and reference SQL. A coverage check prevents those declarations
from drifting apart. The existing differential, routing and compiler assertions
remain in place. Fiscal references also fingerprint `dim_fiscal`, so changing its
bucket boundaries requires checking the frozen fiscal answers.

Ratio references explicitly divide by `DOUBLE PRECISION`, matching the runtime's
floating division on both backends. Decimal amounts remain exact. Native averages
can differ in representation: New York's November average is 22/3, a floating
value on DuckDB and a longer `NUMERIC` value on Postgres. That case separately
freezes Postgres's exact rows under `expected_rows_by_backend.postgres`. Both the
engine check and the unwaivable reference self-check select those rows; neither
rounds nor uses a tolerance.

Planner cases use `intent` and an optional `partial_query` supplied to `plan`.
An `answer` must be execute-ready and return the frozen rows. A `clarify` must
withhold execution, explain the unresolved choice, and fail validation with the
declared first error code. Its `clarify.options` lists exact option IDs in order;
the question must be nonempty and every option's replacement `where` must validate.
A `refuse` must withhold execution and report the declared first error code;
a supplied draft must fail validation with that code too. Merely returning low
confidence cannot satisfy either expectation.

`known_wrong` allows only `engine` and `planner`, each with a nonempty reason.
It marks only that check as a strict expected failure; reference SQL and hygiene
always pass. Remove the marker when its fix makes the check pass.

After an intentional seed change, run
`uv run python -m tests.integration.correctness.answer_ledger fingerprint`
and update `fixture.data_sha256`. Only the listed tables contribute, so adding an
unrelated table does not change the fingerprint.
