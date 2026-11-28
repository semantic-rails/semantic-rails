# Answer evals

`semantic-rails eval` checks that a package still gives the answers it should. A package
author freezes them in `tests/answers.yml`: the rows a governed call must return, and the
refusal code for a call that must not answer. CI runs `eval` on each change to the
package, so a model change that alters a number fails before it merges.

```bash
uv run semantic-rails eval --path path/to/package
```

`eval` runs every answer against the package's configured warehouse, or its DuckDB seed for
the bundled examples. The calls are fixed, so a run is deterministic and no model is
involved.

## The answers file

`tests/answers.yml` holds a `tests` mapping of named entries. Each entry is either an
answer or a refusal.

```yaml
now: '2024-06-01'            # optional: relative windows resolve against it
tests:
  sales/repeat-orders-by-store:
    kind: query_matches_snapshot
    expect: answer
    why: A repeat customer has more than one lifetime order.
    query:
      select: [{expression: metric.sales.repeat_customer_orders, as: repeat_orders}]
      group_by: [dimension.jaffle_store_name]
    expected_rows:
      - {dimension.jaffle_store_name: Brooklyn, repeat_orders: 21460}
      - {dimension.jaffle_store_name: Philadelphia, repeat_orders: 38186}

  sales/partner-revenue-refuses:
    kind: validate_fails_with_code
    expect: refuse
    code: POLICY_DENIED
    why: External partners never see raw revenue.
    query:
      select: [{expression: measure.jaffle.revenue_usd, as: revenue}]
    context: {audience: external_partner}
```

| Key | Meaning |
| --- | --- |
| `expect` | `answer`, `refuse` or `clarify`. |
| `kind` | The package test that checks the entry: `query_matches_snapshot` for an answer, `validate_fails_with_code` for a refusal or clarification. |
| `query` | The [query call](QUERY_CALL.md), exactly as a client sends it. |
| `expected_rows` | An answer's rows. Rows compare as a multiset. If the call has an `order_by`, their order must match too. A series' `period_partial` flag is compared only when the rows include it. |
| `expected_rows_by_backend` | Optional. Rows for one warehouse that represents a value differently, keyed by warehouse (for example `postgres`). |
| `code` | The refusal code a `refuse` or `clarify` entry must return. The match is on the code alone, never on the message. |
| `clarify.options` | Optional. The option ids a clarification must offer, in order. |
| `now` | The instant relative windows resolve against. An entry's own `now` wins over the file's, and both win over `--now`. |
| `context` | Who asks. Its `attributes` are treated as trusted attributes from the host, so a row filter that reads them applies. |
| `known_wrong` | `{engine: reason}` marks an answer that is still wrong. A failing entry is reported as known wrong and doesn't change the exit code. Once it passes, `eval` fails until you remove the marker. |
| `why`, `cites`, `tags` | Notes for reviewers. `eval` doesn't check them. |

Numbers in `expected_rows` load as exact decimals, never as binary floats. Each entry runs
through the same package test runner as `semantic-rails test-package`, so both commands
agree on whether an entry passes. Files under `tests/` other than `answers.yml` are
`test-package`'s, and `eval` doesn't read them.

The file uses the engine's own correctness answer format
(`tests/integration/correctness/shop/tests/answers.yml`): every entry of that ledger is a
valid entry here. The ledger also has keys that only its test harness checks:
`reference_sql`, `intent` and planner drafts, and `variant`. `eval` doesn't check them, and
it skips an entry with a `variant`, which needs a package the harness rewrites.

## Scoring

A wrong number counts against a change more heavily than a missing one.

| Outcome | When |
| --- | --- |
| `failed` | The rows differ. A call that must refuse returns rows. A refusal comes back with a different code, or a clarification offers different options. The warehouse call errors. The entry can't be read. |
| `missed` | A call refuses where rows were expected. |
| `known_wrong` | An entry marked `known_wrong.engine` still fails. |
| `skipped` | The entry needs the correctness harness (`variant`). |
| `passed` | Everything else. |

| Exit code | Meaning |
| --- | --- |
| 0 | Every entry passed (or is known wrong or skipped). |
| 1 | At least one failure, or the package or answers file can't be loaded. With `--strict`, any miss too. |
| 2 | Misses only. |

A failure names the entry and its call. Wrong rows show each differing cell beside the
cells the row shares with what was expected, which are usually its group:

```text
FAIL  sales/repeat-orders-by-store: wrong rows
      call: {"select": [{"expression": "metric.sales.repeat_customer_orders", ...}], ...}
      row {"dimension.jaffle_store_name": "Brooklyn"}: repeat_orders expected 21460, got 21438
5 answers: 4 passed, 1 failed. Exit 1.
```

Rows that match no expected row are listed as `unexpected row`, and expected rows that are
absent as `missing row`. For an ordered call, rows pair by position.

## Options

| Option | Effect |
| --- | --- |
| `--package ID` / `--path PATH` | The package to evaluate (one is required). |
| `--answers PATH` | Another answers file instead of `tests/answers.yml`. |
| `--strict` | A miss exits 1, as a failure does. |
| `--json` | Print the report as JSON: `exit_code`, `summary` (count per outcome), and `answers`, one result per entry with its `outcome`, `reason`, `query` and `details` (`changed_rows`, `missing_rows`, `unexpected_rows`, or the refusal). |
| `--now INSTANT` | The default `now` for entries and files that don't set one. |

## In CI

```yaml
- run: uv run semantic-rails eval --path packages/sales --strict
```

Without `--strict`, a step can treat exit 2 as a warning, so a lost answer is reported
but doesn't block the merge, while a wrong one does.
