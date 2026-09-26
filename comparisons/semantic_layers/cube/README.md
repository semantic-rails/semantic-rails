# Cube Comparison Pack

Cube Core runs live on the shared DuckDB, with its model under `model/cubes/` (one cube per
file) and one query per question under `queries/`: a REST query (`.json`), or an SQL API query
(`.sql`) for six frozen-model questions.

- `@cubejs-backend/server` `1.7.45` and `@cubejs-backend/duckdb-driver` `1.7.45` (exact
  versions in `package.json`, locked in `package-lock.json`); the driver runs DuckDB through
  `@duckdb/node-api` `1.5.5-r.5`.
- Node.js 22 or later (Cube 1.7 dropped Node 20); the recorded run used Node 24.6.
- SQL is planned by Tesseract, Cube's default planner since 1.7. Multi-stage measures and
  multi-fact queries need it.

## Install

```bash
cd comparisons/semantic_layers/cube
CUBESTORE_SKIP_POST_INSTALL=true npm ci --ignore-scripts
npm rebuild @cubejs-backend/native
```

`npm ci --ignore-scripts` installs the locked packages from the npm registry and runs no
install script. The one install script the pack needs is `@cubejs-backend/native`'s
`postinstall`, which `npm rebuild @cubejs-backend/native` runs alone: it downloads Cube's
prebuilt native binary from Cube's GitHub releases, outside the lockfile's integrity hashes,
and Cube 1.7 doesn't start without it. The release tarball is extracted into the package, so
`index.js` pins the sha256 of the whole installed `node_modules/@cubejs-backend/native`
package (every file's path and bytes, binary and loader alike) and refuses to start if it
differs:

| Platform | Release asset | `native/index.node` sha256 | Package tree sha256 (pinned) |
| --- | --- | --- | --- |
| darwin-arm64 | `https://github.com/cube-js/cube/releases/download/v1.7.45/native-darwin-arm64-unknown-fallback.tar.gz` | `b174bb6ea896cf4d5b6446d5c06a0217d232050fd343b8264a8d8c39af3cd56b` | `2d56629d47ef78fc49855f958bccb186778d06312f7d4efcfc47073e12b8b7ba` |

Cube therefore runs only on darwin-arm64 until another platform's tree hash is checked and
added to `index.js`. The pack
doesn't use Cube Store, so its `postinstall` (a second GitHub download) never runs, and
`CUBESTORE_SKIP_POST_INSTALL=true` keeps it skipped if scripts are ever enabled. The other
install scripts in the lockfile (`es5-ext`, `fsevents`) aren't needed. This setup is for local
runs only.

`npm audit --audit-level=high` reports 0 high and 0 critical advisories. It reports 2 moderate
(`uuid` under `gaxios`) and 5 low (`elliptic`, through `jwk-to-pem` in Cube's API gateway; npm
has no fix). `npm-audit.json` records that report with the sha256 of the `package-lock.json`
it audited, and CI checks the install surface offline, failing if the lockfile changed since
the audit. After changing the lockfile, re-record the audit (this runs `npm audit`):

```bash
python3 comparisons/semantic_layers/cube/scripts/verify_evidence.py            # offline check
python3 comparisons/semantic_layers/cube/scripts/verify_evidence.py --record   # re-audit
```

## Start Cube (for a benchmark or by hand)

```bash
cd comparisons/semantic_layers/cube && CUBEJS_API_SECRET="$(openssl rand -hex 32)" npm start
```

Cube serves its REST API at `http://localhost:4000/cubejs-api/v1` (`/meta`, `/sql`, `/load`,
and `/cubesql` for SQL API queries). `index.js` fixes everything else:

- Production mode: no dev server or Playground (it refuses to start with `CUBEJS_DEV_MODE` set or a
  `.env` file present), and every request needs an `Authorization` header carrying an HS256 JWT
  signed with `CUBEJS_API_SECRET` (`token()` in `scripts/run_questions.py` makes one). Cube
  listens on every interface (it has no bind option), so keep the secret private; cross-origin
  browser requests are refused. `scripts/verify_evidence.py` fails CI if `index.js` loses any
  of these dev-server guards.
- `TZ=UTC` and DuckDB `SET TimeZone = 'UTC'`: Cube's SQL casts time dimensions through
  `timestamptz`, so results would otherwise depend on the machine's time zone.
- DuckDB opens `:memory:` and attaches `../shared/data/jaffle_comparison.duckdb` read-only
  through the driver's `initSql` option (its documented settings have no read-only mode).
  While Cube runs, other processes can open the file read-only, but not read-write.
- The SQL API is on, because `/cubesql` needs it. Cube starts it only with a Postgres-protocol
  port, so it also listens on port 15432 on every interface, with user `cube` and a random
  password generated at each start and never shown; the runner uses only `/cubesql`, with the
  same JWT. `CUBESQL_FAIL_ON_LIMITLESS_POST_PROCESSING` makes Cube refuse an SQL API query it
  would post-process over a Cube query with no row limit. Cube still caps the Cube query it
  post-processes, and a pushed-down query's result, at 50,000 rows; the runner refuses a result
  that reaches the cap, but it can't see an inner Cube query's row count.
- An in-memory cache and queue, no Cube Store and no pre-aggregations. Cube caches each result
  in memory and re-checks it every 10 seconds; pass `cache=no-cache` on `/load` to skip it.
- Overrides when started by hand: `PORT` and `CUBE_DUCKDB_PATH` (another DuckDB file).

## Run the pack

```bash
uv run python comparisons/semantic_layers/cube/scripts/run_questions.py
```

The runner starts `node index.js` with a fresh random API secret and no other environment than
`PATH`, `HOME` and `TMPDIR`, so no inherited `CUBEJS_*`, `PORT` or `CUBE_DUCKDB_PATH` setting
changes the run. It waits for `/meta`, saves `/sql` and `/load` for each query under
`shared/results/cube/` (for an SQL API query, `/sql` with `format=sql` and the `/cubesql`
rows in `/load`'s shape), and stops Cube, killing it if it hasn't exited 30 seconds after the
stop signal. `summary.json` records the versions, the dataset
fingerprint and whether each question executed; the rubric assigns the labels.

## Modeling

Every cube reads one `comparison_*` view with `sql_table`; the model has no SQL-defined cube.

- q01-q04, q06, q07: measures, dimensions and joins. q04's average order value is revenue over
  orders; q06 counts orders carrying the seed's `is_new_customer_order` flag, as every layer does.
- q05: one query for `orders.orders` and `order_items.item_revenue_usd`. Cube aggregates each
  measure on its own cube and joins the results on store and month, so the order count doesn't
  fan out and orders without items still count.
- q08, q16: `orders` and `order_lifecycle` join `customer_history` with a join condition that
  checks the validity window on `ordered_at` or `delivered_at`. Orders with no valid row keep a
  null segment.
- q09, q15: `storefront_sessions` joins the same customer's orders placed within 7 days after
  the session start (q15 also requires the same store). A subquery dimension counts each
  session's matching orders; the conversion rate is sessions with at least one over all
  sessions. q15's join targets `same_store_orders`, which `extends: orders`, because a cube
  joins another cube only once.
- q10, q13, q14: multi-stage measures. `customer_month_orders` counts orders at a fixed grain
  (`grain.keep_only` customer and calendar month); the outer measure adds that grain to the
  query's (`grain.include`) and sums the orders, or q14's revenue, of groups with more than 10.
  The query's own grain can differ, as q13's day does. q14 adds the store to both grains.
- q11, q12: subquery dimensions on `customers` compute each customer's lifetime order count and
  spend from the `orders` measures, and the queries filter on them. The precomputed
  `lifetime_*` columns of `comparison_customers` aren't read.
- q17-q24, the frozen-model questions, run with this model unchanged. q24 is q12's REST query
  with a 1000 USD threshold. q22 is an SQL API query of `AVG` and `MAX` over
  `order_items.item_revenue_usd`, a `sum` measure: Cube pushes them down as the average and
  maximum of the measure's row expression, which is the question's rule, and the query selects
  members only, so it is `native`. q18-q21 and q23 are SQL API queries that wrap a Cube query in
  a derived table: a 50-minute flag over the declared 7-day same-store join, weighted by each
  group's session count; a moving sum and `LAG` over monthly revenue; a `CASE` over each order's
  revenue (an ungrouped query of the revenue measure); and a filter on each customer-month's
  order count. The rubric labels them `workaround`, since the logic is SQL around Cube's
  members. Cube pushes q19-q21 and q23 down to DuckDB whole; it post-processes q18 over a Cube
  query result it caps at 50,000 rows (here, the 10 sessions' same-store orders within 7 days),
  and refuses to when q18 is sorted. The moving
  sum and `LAG` step over month rows, which equals the calendar rule only because every month has
  orders (the SQL API refuses a `RANGE` frame with an interval). q17 needs a model change: the
  7-day window is on a declared join, and the SQL API joins cubes only along declared joins, so
  it can't reach orders more than 7 days after a session (`../shared/frozen_model.yml` has each
  reason and its documentation).

## Workarounds

None of q01-q16 needs hand-written SQL or a precomputed column (the frozen-model SQL API answers
above are workarounds). Two modeling detours remain:

- `customers.lifetime_orders` counts `orders.order_count` (`type: count`) rather than
  `orders.orders`. When a query selects `orders.orders` and filters on a subquery dimension
  over that same measure, Cube 1.7.45 renders the measure as the subquery's column, and DuckDB
  rejects the SQL. The legacy planner (`CUBEJS_TESSERACT_SQL_PLANNER=false`) plans q11 correctly
  but ignores the multi-stage `grain`, so q10, q13 and q14 need Tesseract.
- `same_store_orders` exists only to join orders a second time with other conditions (q15).
