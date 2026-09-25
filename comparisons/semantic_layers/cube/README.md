# Cube Comparison Pack

Cube Core runs live on the shared DuckDB, with its model under `model/cubes/` (one cube per
file) and one REST query per question under `queries/`.

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
and Cube 1.7 doesn't start without it. `index.js` pins that binary by sha256 and refuses to
start if the installed one differs:

| Platform | Release asset | `native/index.node` sha256 |
| --- | --- | --- |
| darwin-arm64 | `https://github.com/cube-js/cube/releases/download/v1.7.45/native-darwin-arm64-unknown-fallback.tar.gz` | `b174bb6ea896cf4d5b6446d5c06a0217d232050fd343b8264a8d8c39af3cd56b` |

Another platform needs its own asset's hash added to `index.js` after checking it. The pack
doesn't use Cube Store, so its `postinstall` (a second GitHub download) never runs, and
`CUBESTORE_SKIP_POST_INSTALL=true` keeps it skipped if scripts are ever enabled. The other
install scripts in the lockfile (`es5-ext`, `fsevents`) aren't needed. This setup is for local
runs only.

`npm audit --audit-level=high` reports 0 high and 0 critical advisories. It reports 2 moderate
(`uuid` under `gaxios`) and 5 low (`elliptic`, through `jwk-to-pem` in Cube's API gateway; npm
has no fix). `npm-audit.json` records that report, and CI checks the install surface offline:

```bash
python3 comparisons/semantic_layers/cube/scripts/verify_evidence.py
```

## Start Cube (for a benchmark or by hand)

```bash
cd comparisons/semantic_layers/cube && npm start
```

Cube serves its REST API at `http://localhost:4000/cubejs-api/v1` (`/meta`, `/sql`, `/load`).
`index.js` fixes everything else:

- Dev mode, so requests need no token and the Playground is at `http://localhost:4000`.
- `TZ=UTC` and DuckDB `SET TimeZone = 'UTC'`: Cube's SQL casts time dimensions through
  `timestamptz`, so results would otherwise depend on the machine's time zone.
- DuckDB opens `:memory:` and attaches `../shared/data/jaffle_comparison.duckdb` read-only
  through the driver's `initSql` option (its documented settings have no read-only mode).
  While Cube runs, other processes can open the file read-only, but not read-write.
- An in-memory cache and queue, no Cube Store and no pre-aggregations. Cube caches each result
  in memory and re-checks it every 10 seconds; pass `cache=no-cache` on `/load` to skip it.
- Overrides: `PORT`, `CUBE_DUCKDB_PATH` (another DuckDB file) and `CUBEJS_API_SECRET`.

## Run the pack

```bash
uv run python comparisons/semantic_layers/cube/scripts/run_questions.py
```

The runner starts `node index.js`, waits for `/meta`, saves `/sql` and `/load` for each query
under `shared/results/cube/`, and stops Cube. `summary.json` records the versions, the dataset
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

## Workarounds

No question needs hand-written SQL or a precomputed column. Two modeling detours remain:

- `customers.lifetime_orders` counts `orders.order_count` (`type: count`) rather than
  `orders.orders`. When a query selects `orders.orders` and filters on a subquery dimension
  over that same measure, Cube 1.7.45 renders the measure as the subquery's column, and DuckDB
  rejects the SQL. The legacy planner (`CUBEJS_TESSERACT_SQL_PLANNER=false`) plans q11 correctly
  but ignores the multi-stage `grain`, so q10, q13 and q14 need Tesseract.
- `same_store_orders` exists only to join orders a second time with other conditions (q15).
