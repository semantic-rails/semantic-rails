// Cube's SQL casts time dimensions through timestamptz, which DuckDB reads in its session
// time zone (taken from TZ). Pin it so results don't depend on the machine.
process.env.TZ = "UTC";
// Production mode: no dev server or Playground routes, and every API request needs a JWT
// signed with the API secret.
process.env.NODE_ENV = "production";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

// @cubejs-backend/native's postinstall downloads its binary from Cube's GitHub releases
// (native-<platform>-<arch>-unknown-fallback.tar.gz for v1.7.45) and extracts it into the
// package, outside package-lock.json's integrity hashes. Pin the whole installed package,
// binary and loader, and refuse to start on anything else.
const NATIVE_TREE_SHA256 = {
  "darwin-arm64": "2d56629d47ef78fc49855f958bccb186778d06312f7d4efcfc47073e12b8b7ba",
};

function treeSha256(dir) {
  const hash = crypto.createHash("sha256");
  const walk = (relative) => {
    for (const name of fs.readdirSync(path.join(dir, relative)).sort()) {
      const file = path.posix.join(relative, name);
      if (fs.statSync(path.join(dir, file)).isDirectory()) walk(file);
      else hash.update(`${file}\0`).update(fs.readFileSync(path.join(dir, file)));
    }
  };
  walk("");
  return hash.digest("hex");
}

const platform = `${process.platform}-${process.arch}`;
const native = path.join(__dirname, "node_modules/@cubejs-backend/native");
const digest = fs.existsSync(native) ? treeSha256(native) : "missing";
if (digest !== NATIVE_TREE_SHA256[platform]) {
  console.error(
    `@cubejs-backend/native for ${platform} hashes to ${digest}, not the pinned ` +
      `${NATIVE_TREE_SHA256[platform] || "(none recorded)"}; see README.md.`,
  );
  process.exit(1);
}
const apiSecret = process.env.CUBEJS_API_SECRET;
if (!apiSecret) {
  console.error("Set CUBEJS_API_SECRET; each request needs a JWT signed with it (README.md).");
  process.exit(1);
}

// Cube turns dev mode (the dev server and its unauthenticated Playground routes) back on with
// CUBEJS_DEV_MODE, which @cubejs-backend/server also loads from a .env file in the working
// directory. Refuse both, then load the server.
if (fs.existsSync(path.join(process.cwd(), ".env"))) {
  console.error(`Remove ${path.join(process.cwd(), ".env")}; this setup takes no .env file.`);
  process.exit(1);
}
const CubejsServer = require("@cubejs-backend/server");
if (process.env.CUBEJS_DEV_MODE !== undefined) {
  console.error("Unset CUBEJS_DEV_MODE; this setup runs Cube in production mode only.");
  process.exit(1);
}

// An SQL API query that Cube would post-process with a Cube query result cut off at its row
// limit fails instead. (Pushed-down queries still end in that limit; the runner refuses a
// result that reaches it.)
process.env.CUBESQL_FAIL_ON_LIMITLESS_POST_PROCESSING = "true";

const database = path.resolve(
  __dirname,
  process.env.CUBE_DUCKDB_PATH || "../shared/data/jaffle_comparison.duckdb",
);

const server = new CubejsServer({
  apiSecret,
  devServer: false,
  schemaPath: "model",
  telemetry: false,
  // No cross-origin browser access.
  http: { cors: { origin: false } },
  // Queries only: no Cube Store for the cache and queue, and no pre-aggregations.
  cacheAndQueueDriver: "memory",
  // The SQL API, which the runner reaches through the REST endpoint /v1/cubesql with the same
  // JWT. Cube starts it only with a Postgres-protocol port, which also listens on every
  // interface; its password is random per start and never shown, so nothing can log in there.
  pgSqlPort: 15432,
  sqlUser: "cube",
  sqlPassword: crypto.randomBytes(32).toString("hex"),
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
