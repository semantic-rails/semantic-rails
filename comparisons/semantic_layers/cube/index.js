// Cube's SQL casts time dimensions through timestamptz, which DuckDB reads in its session
// time zone (taken from TZ). Pin it so results don't depend on the machine.
process.env.TZ = "UTC";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

// @cubejs-backend/native's postinstall downloads this binary from Cube's GitHub releases,
// outside package-lock.json's integrity hashes. Refuse to start on any other build.
const NATIVE_SHA256 = {
  // https://github.com/cube-js/cube/releases/download/v1.7.45/native-darwin-arm64-unknown-fallback.tar.gz
  "darwin-arm64": "b174bb6ea896cf4d5b6446d5c06a0217d232050fd343b8264a8d8c39af3cd56b",
};
const native = path.join(__dirname, "node_modules/@cubejs-backend/native/native/index.node");
const platform = `${process.platform}-${process.arch}`;
const digest = fs.existsSync(native)
  ? crypto.createHash("sha256").update(fs.readFileSync(native)).digest("hex")
  : "missing";
if (digest !== NATIVE_SHA256[platform]) {
  console.error(
    `Cube's native binary for ${platform} is ${digest}, not the pinned ` +
      `${NATIVE_SHA256[platform] || "(none recorded)"}; see README.md.`,
  );
  process.exit(1);
}

const CubejsServer = require("@cubejs-backend/server");

const database = path.resolve(
  __dirname,
  process.env.CUBE_DUCKDB_PATH || "../shared/data/jaffle_comparison.duckdb",
);

const server = new CubejsServer({
  apiSecret: process.env.CUBEJS_API_SECRET || "comparison-secret",
  schemaPath: "model",
  telemetry: false,
  // Queries only: no Cube Store for the cache and queue, and no pre-aggregations.
  cacheAndQueueDriver: "memory",
  // The driver opens an in-memory DuckDB and attaches the shared file read-only, so other
  // processes can open the file read-only while Cube runs.
  driverFactory: () => ({
    type: "duckdb",
    initSql: `ATTACH '${database.replaceAll("'", "''")}' AS jaffle (READ_ONLY); USE jaffle; SET TimeZone = 'UTC';`,
  }),
});

server.listen().then(({ version, port }) => {
  console.log(`Cube ${version} is listening on ${port}`);
});
