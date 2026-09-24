# MCP Interface

`semantic_rails.mcp` exposes a dependency-free Model Context Protocol interface for the same
stable operations served by `/api/v1/*`. The canonical implementation remains the in-process
`SemanticLayerMCPAdapter`. The ASGI app serves stateless MCP Streamable HTTP at `/mcp`, while the
CLI retains packaged stdio and legacy HTTP/SSE transports for local compatibility.

## Streamable HTTP Endpoint

A self-hosted ASGI process exposes:

```text
http://127.0.0.1:8080/mcp
```

It implements stateless MCP Streamable HTTP with protocol version `2025-11-25`.
`POST /mcp` accepts one JSON-RPC object and returns JSON; notifications return `202` with no body.
`GET /mcp` returns `405` because this transport does not require server-initiated SSE.
When `SEMANTIC_RAILS_API_KEYS` (or `SEMANTIC_RAILS_API_KEY_FILE`) is configured, `/mcp` requires
the same bearer API key as `/api/v1/*` and returns `401` otherwise.
Clients must send:

```text
Content-Type: application/json
Accept: application/json, text/event-stream
MCP-Protocol-Version: 2025-11-25
```

To keep MCP context small, stay on the defaults (`minimal` for
validate/compile/execute, `summary` for catalog) and reach for `compact`/`full`
only when you need explain plans or descriptive rows. Measured sizes for every
tier are in the tables below.

## Runtime Adapter

```python
from semantic_rails.mcp import SemanticLayerMCPAdapter

adapter = SemanticLayerMCPAdapter.from_package("jaffle_shop")
try:
    tools = adapter.list_tools()       # Paid once at connect time.
    # Loop position 0: compact orientation.
    capabilities = adapter.call_tool("capabilities", {})
    # Loop position 1: counts + flat ID list per kind.
    catalog = adapter.call_tool("catalog", {"verbosity": "summary"})
    draft = adapter.call_tool("plan", {"intent": "orders by store", "detail": "query"})
    if draft["status"] == "ok":
        result = adapter.call_tool(
            "execute",
            {"query": draft["best"]["query_ir"], "row_format": "columns"},
        )
finally:
    adapter.close()
```

The adapter returns plain dictionaries with the same additive envelope fields used by the HTTP
API:

- `ok`
- `status`
- `api_version`
- `request_id`
- `package_id`
- `warnings`
- `errors`
- `recovery_hints`
- `timing_ms`

Every `tools/list` definition publishes an `outputSchema` for this envelope and
MCP-standard annotations (`readOnlyHint`, `destructiveHint`,
`idempotentHint`, and `openWorldHint`). The complete generated contract is
packaged as `semantic_rails/contracts/query_mcp.v1.json`; CI compares it with
the executable definitions so tool/schema drift cannot be merged silently.

## Tools

The tool names mirror the public API operations:

- `capabilities` (loop position 0 — compact orientation)
- `catalog` (loop position 1 — counts + IDs at `verbosity=summary`)
- `discover`
- `inspect`
- `build-options`
- `valid-values`
- `plan`
- `validate`
- `compile`
- `execute` (`/api/v1/query`)
- `segment-validate`
- `segment-explain`
- `segment-preview`

For first-time question-answering tests, make the session aware of the core loop before
asking for rows: `capabilities`, `catalog`, `discover`, `plan`, `validate`, `compile`,
and `execute`. Some MCP hosts lazily expose only tools that match the user's first
request; in those hosts, start with a setup prompt such as:

```text
Load the Semantic Rails MCP question-answering tools: capabilities, catalog, discover,
plan, validate, compile, execute. Then use plan(detail="query") and
execute(row_format="columns") to answer the question; call validate when editing IR
or when diagnostics are needed.
```

`tools/list` is paid once at connect time, before loop position 0. To keep that
cold-start payload bounded, the IR cheat-sheet and the full Query-IR time-block
schema ship once on the `validate` tool description; `compile`, `execute`, and
the other IR-accepting tools point at it instead of repeating it. Catalog payload
size varies materially with package size and selected verbosity.

### Catalog Verbosity Tiers

`catalog` accepts four `verbosity` values, sized by use case:

| Verbosity | Shape | Relative payload | Use case |
|---|---|---|---|
| `summary` (default) | counts + flat ID list per kind + capabilities | Smallest | Cold-start orientation |
| `minimal` | skeleton rows `{id, kind, name, label, available}` + counts | Small | "Show me what exists with labels" |
| `compact` | descriptive rows capped at 200/kind + counts + counts_total | Larger, bounded | Browsing with descriptions |
| `full` | uncapped rows + alias_index + aliases | Largest, package-dependent | Debugging, code-gen, exhaustive scans |

The MCP `catalog` tool defaults to `verbosity=summary` (the tool description says so too).
Anything above `summary` is a large envelope — request `compact` or `full` only when you
genuinely need descriptions or the alias index.

`alias_index` and `aliases` ship only at `verbosity=full` — agents who need typo-resolution opt in explicitly.

`validate`, `compile`, and `execute` accept either `{"query": {...}}` or a Query IR
object directly. Metadata tools accept the same request fields documented in
[QUERY_API.md](QUERY_API.md), including optional `policy_context`.

`plan` is the only public natural-language intent tool. Stable v1 MCP calls without a
`detail` argument keep `detail="best"`: `status`, `best.query_ir`, `intent_ir`, `best.trace`,
`next`, and a `why` or `warnings` entry for any part of the question the draft doesn't honor.
For the lowest-token QA loop, pass `detail="query"` to return `status`, `best.query_ir` and
any `why` or `warnings`, then forward `best.query_ir` to `execute` with
`row_format="columns"` and an explicit `max_rows`. When `status="ok"`, the draft has already
paid validation cost, so call `validate` again only when you are editing the IR or
need full diagnostics.

A draft that validates can still leave out part of the question. `plan` returns
`low_confidence` with `why.code="PLAN_INTENT_COVERAGE_GAP"` when the draft:

- carries no time window, or a different one, where the question names one
  (`time_window_unrealized`);
- loses a ranking's stated limit, sort direction or selected measure, cannot identify the
  ranked measure unambiguously, or doesn't group by what is ranked (`ranking_unrealized`),
  including count-free requests such as "top stores by revenue";
- excludes a value requested positively, or cannot prove that its filter keeps or drops
  each named value with the requested polarity (`filter_values_unrealized`). Scalar `=`/`!=`
  and scalar or list `IN`/`NOT IN` can prove it; list-valued `=`/`!=`, empty membership
  lists and pattern filters cannot. The guard intersects top-level filters on the same
  field, accounts for exclusions, and compares draft literals to stored values exactly;
  catalog labels and aliases only identify values named in the question. Nested filter
  scopes do not prove an outer filter's result. Grouping does not cure an uncertain filter;
- combines top-level filters on one field so no value can survive, which returns no rows
  (`contradictory_filters`);
- misses a negation, a prior-period comparison or one of several named subjects.

`why.details.gaps` names each clause. Question words the draft uses nowhere, other than
framing words, time phrases the planner read, and counts, come back as a
`PLAN_UNMATCHED_TERMS` warning with up to eight of them in `details.terms`; check them
before executing. If a
validating fallback would change the target, grouping, qualification/cohort,
filters, or time scope, `plan` returns `low_confidence` with
`why.code="PLAN_FALLBACK_SEMANTIC_DRIFT"` instead of silently promoting it.
`plan` resolves a time window only when the question names exactly one, in a form it reads
unambiguously: a year after "in", "for" or "during", consecutive years, a quarter or half with a
year, a month or month range with a year, days with a year, an ISO date, or a relative window
("last 7 days"). "and" joins a range only after "between": "between March and May 2017" is a
range, while "March and May 2017" names two months. Anything else, such as a bound ("before
2017", "since March 2017"), a qualifier ("early 2017"), a comparison ("2017 vs 2016", "2017 over
2016"), a numeric date (4/3/2017), two periods joined by "and", or two windows at once (such as
"last month and this month"), returns
`low_confidence` with `why.code="TIME_WINDOW_UNRESOLVED"` and the phrases it couldn't resolve,
never a window narrowed or widened to the nearest form that parses. A total over a window gets
one bucket when one calendar grain holds the window; an explicit grain ("monthly") wins. When a
draft can't take the window's start, because the metric looks back over earlier periods or the
question compares with an earlier period, `plan` keeps the end and returns
`why.code="TIME_WINDOW_START_DROPPED"` with the start to filter by.
Questions longer than 2,000 characters are not partially parsed for time: unless the caller
provides a complete window in `query.time` (both `start` and `end`, or a relative `range`),
they return `TIME_WINDOW_UNRESOLVED` with a request to shorten the question or supply those
bounds. Include the selected `temporal_role` and `grain` in that time block. A date or
qualifier beyond the limit therefore cannot silently disappear from an otherwise ready draft.
Use `detail="full"` only when you need alternatives or blocked drafts.

Use `compile` and read its `explain` payload to review relationship paths before executing a
cross-entity query. `explain.chosen_paths` is keyed by target entity ID; each entry carries
`selected` (the chosen relationship path), `candidates` (every considered path), and
`contracts` (the relationship contracts along the selected path) — i.e.
`explain.chosen_paths["entity.jaffle_store"].candidates`, not `explain.candidates`.
Note the MCP `compile` default (`verbosity=minimal`) strips `explain`; pass
`verbosity="compact"` or `"full"` when you need it.

### Query Verbosity Tiers (validate / compile / execute)

At the MCP adapter boundary, `validate`, `compile`, and `execute` default to
`verbosity=minimal`. An explicit `verbosity` argument always wins, and error envelopes
(`ok: false`) inherit the same default. This is an MCP-only default — the HTTP `/api/v1/*`
default remains `compact`.

| Verbosity | What's kept | Size (jaffle, measured*) | When to use |
|---|---|---|---|
| `minimal` (MCP default) | `{ok, status, errors, warnings, recovery_hints}`; `compile` also keeps `rendered_sql`; `execute` also keeps `rows` + `row_count` | ~0.7KB / ~1.9KB / ~2.4KB | Tight agent loops with a tool-output cap |
| `compact` (HTTP default) | includes compact `trace`; drops top-level `physical_plan`, `performance_plan`, `semantic_summary`, `compile_stats`; strips `output_columns.lineage` | ~88KB / ~94KB / ~96KB | Diagnostics, `explain` review |
| `full` | includes compact `trace` plus every field, including the heavy plan trees | ~97KB / ~114KB / ~116KB | Debugging, code-gen |

*Sizes are validate / compile / execute for a representative 5-row jaffle query (revenue by
store by month); they scale with query complexity and row count.

`sql_profile="off"` drops `rendered_sql` and `sql_plan` at any verbosity for callers that want the semantic envelope without the SQL.

`execute` also accepts `row_format="columns"`. The default `row_format="records"`
keeps `rows` as objects (`[{...}]`). The opt-in columnar form returns
`columns: [...]`, `rows: [[...]]`, `row_format: "columns"`, and the same
`row_count`, warnings, and errors while avoiding repeated field names.

Stable v1 MCP `execute` calls without `max_rows` keep the caller's existing Query IR row limit;
they do not add a response cap. Pass `max_rows=200` (or another value up to 100,000) to bound
the response. A larger result comes back with `truncated: true`, `total_row_count` and an
`EXECUTE_ROWS_TRUNCATED` warning that says how to narrow the query. With an explicit cap,
execute asks the warehouse for up to 10,000 rows to count them, so
`total_row_count` is `null` when more rows exist than were fetched. Some warehouse adapters fetch
the whole result and then clip it; the cap bounds the response, not the warehouse work. Pass a
larger `max_rows` to see more.

A `limits.max_rows` inside the query is an operator's fetch ceiling. It can lower an explicit
`max_rows` cap (and then `total_row_count` is `null` once it is reached), but it never raises it.
The `query` that execute echoes back carries the caller's own `limits`; a transport-level
`max_rows` does not become part of that query. The HTTP `/api/v1/query` endpoint also leaves
the response uncapped unless the query itself sets a limit.

Query patches returned by `discover`, `inspect` and `build-options` (at every builder step) contain
only Query IR fields and validate as returned, except that a temporal role offered at the
`time` step may not be one the selected measure uses. They never carry the caller's `policy_context` or
the tool's own arguments, so pass the policy context again on the call that uses a patch. A patch
that selects a metric needing a time window carries the metric's default one, and a `percentile`
aggregation option carries `p: 0.5`. These tools read Query IR only from their `query` argument,
not from Query IR fields passed at the top level.

### Semantic Trace

`plan.best.trace` and `compile`/`execute` with `verbosity="compact"` or
`"full"` expose a compact, human-facing explanation of what the runtime did.
The trace includes intent slots, selected subjects, filters, groupings,
relationship paths, root entity, rewrite/fanout status, fallback decision, and
whether SQL is present. It intentionally omits full physical plans and is not
stored server-side.

### Soft-fail warning codes

Tools surface non-blocking signals in the top-level `warnings` array — read it before concluding a response was silent. The codes the adapter emits:

| Code | Tool(s) | Meaning |
|---|---|---|
| `DISCOVER_NO_TERMS` | `discover` | `terms` was empty/whitespace; results are default-ordered, not ranked |
| `DISCOVER_TERMS_COERCED` | `discover` | `terms` was a non-string (int/float/bool); coerced to a string |
| `DISCOVER_UNKNOWN_ARG` | `discover` | Unknown argument (incl. `term`/`kind` typos); the value was ignored |
| `DISCOVER_UNKNOWN_KIND` | `discover` | One or more `kinds` values aren't valid object kinds; ignored |
| `BUILD_OPTIONS_UNKNOWN_ARG` | `build-options` | Unknown argument (incl. `object_id`/`terms` typos); the value was ignored |
| `INSPECT_UNKNOWN_ARG` | `inspect` | Unknown argument received; the value was ignored |
| `VALID_VALUES_UNKNOWN_ARG` | `valid-values` | Unknown argument received; the value was ignored |
| `PLAN_UNKNOWN_ARG` | `plan` | Unknown argument received; the value was ignored |
| `VALIDATE_UNKNOWN_ARG` | `validate` | Unknown argument received; the value was ignored |
| `COMPILE_UNKNOWN_ARG` | `compile` | Unknown argument received; the value was ignored |
| `EXECUTE_UNKNOWN_ARG` | `execute` | Unknown argument received; the value was ignored |
| `VALID_VALUES_NO_DOMAIN` | `valid-values` | Dimension has no declared value domain; flip `allow_live_query=true` to probe |
| `EXECUTE_EMPTY_RESULT` | `execute` | Returned 0 rows with no user filters — verify the measure/time range |
| `PLAN_UNMATCHED_TERMS` | `plan` | The draft uses none of `details.terms` — check it answers the question before executing |
| `EXECUTE_ROWS_TRUNCATED` | `execute` | Returned `max_rows` of `total_row_count` rows — narrow the query or raise `max_rows` |
| `UNGRAINED_TIME_PROJECTION` | `validate`, `compile`, `execute` | From the runtime: an ungrouped query has a temporal role but no grain, so rows group by the raw timestamp — set `time.grain` |
| `UNGRAINED_GROUPED_TIME_PROJECTION` | `validate`, `compile`, `execute` | The same for a grouped query: each group returns one row per distinct timestamp. Same shape, with a `SET_TIME_GRAIN` recovery hint |
| `SEMANTIC_CAVEAT_APPLIED` | `validate`, `compile`, `execute` | Package-authored advisory context matched the query; interpret affected results with that context |
| `SEMANTIC_CAVEATS_TRUNCATED` | `validate`, `compile`, `execute` | More caveats matched than this verbosity returned; increase verbosity to inspect the rest |

Every `*_UNKNOWN_ARG` warning carries `details.received` (the offending key). Most also carry `details.closest_matches` (up to two ranked suggestions via `difflib.get_close_matches`); the special-cased singular/plural typos (e.g. `term` → `terms` on `discover`) carry `details.expected` with the canonical spelling instead.

Errors return `INVALID_MCP_ARGUMENTS` (with `closest_matches` for typo'd keys) when the boundary contract is violated outright — e.g. wrong arg name, wrong type, value outside a declared enum. The full envelope shape is identical across all 13 tools.

### Argument strictness contract

Eight rounds of blind-agent probing found that the 13 tools used to apply three different strictness models when given unknown arguments. The current contract is uniform: every tool either **rejects** unknown args (with a structured error) or **warns** and ignores them (with `details.closest_matches`). No tool silently accepts unknown keys.

| Tool | Unknown-arg behavior | Warning / error code |
|---|---|---|
| `capabilities` | strict-reject | `INVALID_MCP_ARGUMENTS` |
| `catalog` | strict-reject | `INVALID_MCP_ARGUMENTS` |
| `segment-validate` | strict-reject | `INVALID_MCP_ARGUMENTS` |
| `segment-explain` | strict-reject | `INVALID_MCP_ARGUMENTS` |
| `segment-preview` | strict-reject | `INVALID_MCP_ARGUMENTS` |
| `discover` | warn-and-ignore | `DISCOVER_UNKNOWN_ARG` |
| `build-options` | warn-and-ignore | `BUILD_OPTIONS_UNKNOWN_ARG` |
| `inspect` | warn-and-ignore | `INSPECT_UNKNOWN_ARG` |
| `valid-values` | warn-and-ignore | `VALID_VALUES_UNKNOWN_ARG` |
| `plan` | warn-and-ignore | `PLAN_UNKNOWN_ARG` |
| `validate` | warn-and-ignore | `VALIDATE_UNKNOWN_ARG` |
| `compile` | warn-and-ignore | `COMPILE_UNKNOWN_ARG` |
| `execute` | warn-and-ignore | `EXECUTE_UNKNOWN_ARG` |

The warn-and-ignore tools cannot reject all unknown keys because callers legitimately add `policy_context` (every warn-tool) and may pass canonical Query-IR keys (`select`, `time`, `version`, etc.) at top level on `validate`, `compile`, and `execute` — those passthroughs are explicitly part of the contract and never trigger an unknown-arg warning. Query-IR shape errors (e.g. an unknown key *inside* `query`) surface separately as `INVALID_QUERY` from the IR validator in `ast.py`.

## Resources And Prompts

Declarative resources:

- `semantic-rails://capabilities`
- `semantic-rails://catalog/summary`
- `semantic-rails://catalog/full`

Declarative prompts:

- `semantic-rails-query-builder`
- `semantic-rails-query-review`
- `semantic-rails-segment-workflow`

Use `adapter.read_resource(uri)` and `adapter.get_prompt(name, arguments)` to access these
surfaces in process.

## Packaged Server Commands

For a UV-installed local package, use the installed console script and an
absolute package path derived from the project you created with `init`:

```bash
PACKAGE_PATH="$(pwd)/my_package"
semantic-rails mcp setup --path "$PACKAGE_PATH"
```

From a source checkout, prefix commands with `uv run`; `--package jaffle_shop`
loads the bundled synthetic fixture package:

```bash
uv run semantic-rails mcp stdio --package jaffle_shop
uv run semantic-rails mcp doctor --package jaffle_shop
PACKAGE_PATH="$(pwd)/my_package"
uv run semantic-rails mcp stdio --path "$PACKAGE_PATH"
uv run semantic-rails mcp http --package jaffle_shop --host 127.0.0.1 --port 8091
```

`mcp doctor` loads the package and adapter once, confirms the required tools are
registered, and prints the exact stdio/http commands to run next. It does not
bind a port.

Managed local HTTP server commands (POSIX only):

```bash
semantic-rails mcp start --path "$PACKAGE_PATH" --port 8091
semantic-rails mcp status --path "$PACKAGE_PATH"
semantic-rails mcp stop --path "$PACKAGE_PATH"
```

The setup wizard uses this same default server name. Starting the same healthy
configuration is idempotent. If `status` reports a dead registration, stop it
explicitly with `semantic-rails mcp stop --name default`; the interactive
wizard can also remove a dead registration and retry.

Windows users should install the generated stdio client config with
`semantic-rails mcp setup --install --yes`, or run `mcp http` in a foreground
terminal. `mcp doctor` reports the supported lifecycle and prints the matching
commands for the current platform.

The raw server commands are foreground processes. `mcp stdio` is intended for
MCP hosts that launch a subprocess from their config; `mcp http` stays attached
to the terminal until you stop it. Use them directly when a host or another
terminal is managing the process:

```bash
semantic-rails mcp stdio --path "$PACKAGE_PATH"
semantic-rails mcp http --path "$PACKAGE_PATH" --host 127.0.0.1 --port 8091
```

Casual local setup:

```bash
semantic-rails mcp setup --path "$PACKAGE_PATH"
semantic-rails mcp setup --path "$PACKAGE_PATH" --client both --mcp both --install --yes
```

`mcp setup` checks that the package loads, confirms the MCP adapter exposes the
expected tools, and previews or installs stdio entries for Claude Desktop and
Codex. Run it from inside the package directory, pass `--path`, or set a local
profile with `semantic-rails profile init --package-path ./my_package`.

Lower-level client config helpers:

```bash
semantic-rails mcp client-config --path "$PACKAGE_PATH" --client both --mcp both
semantic-rails mcp client-config --path "$PACKAGE_PATH" --client claude --mcp both --install --yes
semantic-rails mcp client-config --path "$PACKAGE_PATH" --client codex --mcp both --install --yes
```

`client-config` writes stdio MCP entries. For Claude Desktop it updates
`claude_desktop_config.json`; for Codex it updates `~/.codex/config.toml`.
Use `--mcp query`, `--mcp architect`, or `--mcp both` depending on whether the
client should answer governed analytics questions, author packages, or do both.

```bash
curl -s http://127.0.0.1:8091/health
curl -s http://127.0.0.1:8091/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-11-25' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | python -c 'import json, sys; tools=json.load(sys.stdin)["result"]["tools"]; print(f"{len(tools)} tools"); print("\n".join("- " + tool["name"] for tool in tools))'
```

This compatibility server accepts JSON-RPC requests at `/mcp` and exposes an SSE endpoint at
`/sse`; it is not the hosted Streamable HTTP transport. When
`SEMANTIC_RAILS_API_KEYS` or `SEMANTIC_RAILS_API_KEY_FILE` is configured, `/mcp` and `/sse` require
`Authorization: Bearer ...`, `X-API-Key`, or `X-Semantic-API-Key`. `/health` stays public.

Pip-installed stdio configuration can be generated from the project directory
so the command and package path match the local machine:

```bash
python - <<'PY'
import json
import shutil
from pathlib import Path

print(json.dumps({
    "mcpServers": {
        "semantic-rails": {
            "command": shutil.which("semantic-rails") or "semantic-rails",
            "args": ["mcp", "stdio", "--path", str(Path("my_package").resolve())],
        }
    }
}, indent=2))
PY
```

Source-checkout stdio configuration:

```json
{
  "mcpServers": {
    "semantic-rails": {
      "command": "uv",
      "args": ["run", "semantic-rails", "mcp", "stdio", "--package", "jaffle_shop"]
    }
  }
}
```

Cursor/Codex-style hosts can use the same stdio command. Hosts that support
Streamable HTTP can use the ASGI `/mcp` endpoint. Local compatibility testing
can use `http://127.0.0.1:8091/mcp` after starting the legacy HTTP transport.

Local profile defaults (`~/.semantic_rails/profiles.yml`) are a human CLI
convenience. MCP host configs should still pass an explicit `--path` or
`--package` so the host is deterministic, and deployed services should use their
own config/vault rather than reading a user's home directory.

## Optional FastMCP stdio Runtime

Importing `semantic_rails.mcp` never imports an external MCP package. A trusted local host can wrap
the adapter with `create_optional_fastmcp_server(adapter)` for stdio. The returned facade rejects
FastMCP's SSE and Streamable HTTP runners because those generic runners cannot supply Semantic
Rails' authenticated request context. Use the built-in ASGI `/mcp` endpoint or
`semantic-rails mcp http` for network transport. The helper imports
`mcp.server.fastmcp.FastMCP` locally and raises a clear error when it is unavailable.

## Structured Error Envelopes

Every error surfaced through the MCP or HTTP transport is wrapped in a structured envelope:

```json
{
  "code": "OBJECT_NOT_FOUND",
  "message": "Unknown object 'measure.jaffle.order_kount'",
  "severity": "error",
  "stage": "mcp",
  "details": {
    "object_id": "measure.jaffle.order_kount",
    "closest_matches": ["measure.jaffle.order_count", "measure.jaffle.large_order_count"]
  },
  "recovery_hints": [
    {
      "kind": "inspect_or_discover",
      "message": "Resolve the object ID through discover or inspect before querying.",
      "closest_matches": ["measure.jaffle.order_count", "measure.jaffle.large_order_count"]
    }
  ]
}
```

Every envelope carries `code` and `message`, plus at least one of `details`, `recovery_hints`, or `closest_matches`. Bare `KeyError` / `AttributeError` leaks are wrapped as `INTERNAL_ERROR` envelopes with a bug-tracker hint so the surface is always actionable.

### Error Code Catalog

| Code | One-line description |
|------|----------------------|
| `AMBIGUOUS_ALIAS` | Alias resolves to multiple semantic objects; pick one from `details.candidates`. |
| `AMBIGUOUS_PATH` | Path between root entity and target is ambiguous; narrow the query. |
| `DUPLICATE_OUTPUT_ALIAS` | Two projected columns share an alias; rename one. |
| `UNSUPPORTED_AGGREGATION` | Aggregation kind is not legal for this measure's class. |
| `INVALID_TEMPORAL_ROLE` | Unknown temporal role; pick one from `details.compatible_temporal_roles`. |
| `INCOMPATIBLE_TEMPORAL_ROLE` | Selected role is not compatible with the chosen measure/metric. |
| `INVALID_TEMPORAL_BINDING` | Time block targets a clock incompatible with a conversion's anchor; filter on `details.anchor_temporal_role` or push the constraint into a conversion metric. |
| `INCOMPATIBLE_CALENDAR` | Selected calendar grain is not supported by the underlying measure. |
| `FANOUT_UNSAFE` | Breakdown crosses a 1-to-many relationship without a pre-aggregation boundary. |
| `ROLLUP_UNSAFE` | Roll-up combines non-additive primitives; declare the aggregation entity or supply sketch metadata. |
| `MEASURE_VALIDITY_BOUNDARY` | Query crosses a declared measure-validity window; split by sub-window. |
| `OUT_OF_SCOPE` | Request isn't a governed-data query; hand off to the recommended tool — the semantic layer compiles governed data queries only. |
| `CUMULATIVE_TIME_FILTER_UNSUPPORTED` | Measure's accumulation semantics forbid the requested time filter. |
| `WINDOWED_TIME_FILTER_UNSUPPORTED` | Time-windowed filter cannot be applied to this query shape. `details.lookback` carries the metric's window; `recovery_hints` carries a `widen_time_window` patch with a concrete `suggested_start` and a `drop_time_start` patch with `{remove: ["time.start"]}`. |
| `MIXED_GRAIN_INVALID` | Query mixes incompatible grains; split or rewrite. |
| `NO_VALID_VALUES_SOURCE` | No `valid_values` source declared for the requested dimension. |
| `REWRITE_NOT_SUPPORTED` | Required rewrite is not implemented; try a simpler shape. |
| `INVALID_EXPRESSION_AST` | Expression AST is malformed; check the position-specific shape. |
| `OBJECT_NOT_FOUND` | Referenced `object_id` does not exist; see `details.closest_matches`. |
| `INVALID_QUERY` | Query IR fails structural validation. |
| `INVALID_CONFIG` | Package config is malformed. |
| `INVALID_METRIC_FILTER` | `metric_filters[]` entry is malformed; check the shape. |
| `INVALID_SEGMENT` | Segment definition is invalid. |
| `MISSING_DEPENDENCY` | Required upstream object is missing. |
| `QUERY_EXECUTION_ERROR` | Warehouse refused or aborted execution. |
| `PATH_NOT_FOUND` | No valid join path between the requested objects. |
| `POLICY_DENIED` | Policy context blocks a referenced object or query cut. |
| `INVALID_METRIC_PREDICATE` | `metric_predicates[]` entry is malformed. |
| `PREDICATE_SCOPE_UNSAFE` | Predicate scope is incompatible with query grain. |
| `PREDICATE_CONTEXT_ENTITY_INCOMPATIBLE` | Predicate context entity disagrees with the surrounding query. |
| `PREDICATE_FILTER_INCOMPATIBLE` | Predicate filter cannot be expressed for the chosen entity. |
| `PREDICATE_GRAIN_UNSAFE` | Predicate would silently change query grain. |
| `PREDICATE_ENTITY_REQUIRED` | Predicate must declare an entity. |
| `PREDICATE_INPUT_REQUIRED` | Predicate is missing a required input. |
| `PREDICATE_NOT_SUPPORTED` | Predicate kind is not supported for this measure. |
| `INVALID_ORDER_BY` | `order_by[]` entry has the wrong shape. |
| `CONVERSION_NOT_SUPPORTED` | Conversion semantics are not supported for this metric. |
| `CONVERSION_ENTITY_REQUIRED` | Conversion must declare an anchor entity. |
| `CONVERSION_WINDOW_REQUIRED` | Conversion is missing a required time window. |
| `CONVERSION_MATCHING_MODE_REQUIRED` | Conversion is missing the matching mode (`first_after`, `last_before`, ...). |
| `UNKNOWN_MCP_PROMPT` | Prompt name isn't in the catalog; see `details.available_prompts`. |
| `UNKNOWN_MCP_RESOURCE` | Resource URI isn't in the catalog; see `details.available_resources`. |
| `UNKNOWN_MCP_TOOL` | Tool name isn't in `tools/list`; see `details.available_tools`. |
| `INVALID_MCP_ARGUMENTS` | Tool arguments don't match the input_schema; `recovery_hints` carries the corrected shape. |
| `INTERNAL_ERROR` | Bare exception reached the boundary; retry once and file a bug if it recurs. |

### Worked Example Envelopes

`OBJECT_NOT_FOUND` on `inspect` with a typo:

```json
{
  "code": "OBJECT_NOT_FOUND",
  "message": "Unknown object 'measure.jaffle.order_kount'",
  "details": {"object_id": "measure.jaffle.order_kount",
              "closest_matches": ["measure.jaffle.order_count"]},
  "recovery_hints": [{"kind": "inspect_or_discover",
                       "closest_matches": ["measure.jaffle.order_count"]}]
}
```

`INVALID_MCP_ARGUMENTS` when `query` is passed as a string:

```json
{
  "code": "INVALID_MCP_ARGUMENTS",
  "message": "Argument 'query' must be a JSON object.",
  "details": {"field": "query", "argument_type": "str"},
  "recovery_hints": [{"kind": "wrap_query_as_object",
                       "closest_valid_query": {"version": 1, "select": []}}]
}
```

`INTERNAL_ERROR` when a bare exception escapes the handler:

```json
{
  "code": "INTERNAL_ERROR",
  "message": "KeyError: 'field'",
  "details": {"exception_type": "KeyError"},
  "recovery_hints": [{"kind": "file_bug_report",
                       "message": "...file a bug at .../issues..."}]
}
```

## Measuring Context Cost

`scripts/mcp_context.py` measures how much context the query MCP costs an agent, and how often
`plan` drafts the right query. It drives the packaged server in process, through the same JSON-RPC
dispatcher as `semantic-rails mcp stdio`, against a throwaway copy of `jaffle_shop` whose DuckDB file
it builds from the seed data. Token counts are `round(chars / 4)` of the JSON a model sees: the
compact `structuredContent` (what Claude Code forwards) and the `content[0].text` channel (what
text-forwarding hosts forward). The proxy needs no tokenizer. Compare runs with each other, not with
provider bills.

```bash
uv run python scripts/mcp_context.py                  # report, then fail on any gate
uv run python scripts/mcp_context.py --markdown       # tables for a PR description
uv run python scripts/mcp_context.py --write-baseline # after an intended change
uv run python scripts/mcp_context.py --eval-file PATH # score a copy of a frozen split
```

`tests/semantic_rails/test_mcp_context.py` runs the same gates in CI. Their data lives in
`tests/semantic_rails/mcp_context/`:

| Gate | Fails when |
|---|---|
| Context budgets (`budgets.json`) | A measured size exceeds its budget by more than 2% (and at least 8 tokens), a count such as the number of tools exceeds its budget at all, a size has no budget, or a budgeted size is no longer measured. |
| Planner accuracy (`plan_accuracy_baseline.json`) | A gold case's `plan(detail="query")` outcome gets worse, or a wrong case gets a slot wrong that it used to get right. |
| Gold answers | A gold query fails, its rows no longer match the frozen answer, or a listed alternative answers differently. |
| Frozen eval set | `eval_jaffle.jsonl` no longer matches `DEV_SET_SHA256` in the script. |

The budgets cover `tools/list`, the `initialize` instructions, the resource and prompt lists, every
resource read, one compact opt-in call per tool (`plan(detail="query")` and
`execute(max_rows=200)`, including a time window without a grain), three metadata calls behind
an authenticated transport, four common mistakes, and two
scripted three-question sessions. Three of the mistakes fail with a specific error code; the fourth,
a misspelled `discover` argument, succeeds with a warning. A scripted call that fails when it should
succeed (or the reverse), or that reports a different code, stops the measurement rather than
counting as a smaller response. A scripted session's queries may only use ids that an earlier call
in the same session returned, so its size measures a path an agent could follow. Architect MCP
tool-list sizes are recorded under `tracked` and are not gated.

A draft is correct when it matches the gold query, or a listed alternative, in every slot that can
change its rows: measures and metrics, grouping, time role, grain, window, `fill`, calendar,
filters, metric filters, temporal role overrides, path policy and limit, plus the sort for
rankings. Its rows must also match the frozen answer. An unanswerable question is answered
correctly when `plan` refuses it as `out_of_scope` or `unrealizable`. Each outcome is one of:

- `pass`: correct, and the response reports `ok` with no warnings.
- `pass_flagged`: correct, but the response still reports a non-`ok` status or a warning: a false
  alarm, which costs the agent a needless repair.
- `wrong_flagged`: wrong, and the status isn't `ok`.
- `wrong_warned`: wrong, with status `ok` but a warning that signals doubt.
- `wrong_silent`: wrong, and the response reports `ok` with no warnings.

A `plan` call that fails outright stops the run instead of being graded. The baseline records
each case's outcome and the slots it gets wrong (`answer` when every slot matches but the rows
don't), so a case that is already wrong can't quietly get worse.

When a change is intended, such as a smaller response or a planner fix, run `--write-baseline` and
commit the updated budgets or outcomes with it. The report lists sizes under budget and cases that
improved, so savings get locked in. A budget moves only when its size moves beyond the tolerance,
and `--write-baseline` refuses to run while a gold answer fails or the eval set has changed.

The release workflows also run `scripts/benchmark_plan.py --gate` over the blind-agent corpus.
That gate checks that plans are actionable, carry the expected IDs and Query IR fields, stay
smaller than `detail="full"`, and hit the compile cache. This one checks drafted queries against
gold answers and tells flagged mistakes from silent ones.

### Eval Set

`eval_jaffle.jsonl` is the frozen development split: 38 questions covering trends, calendar
windows, rankings, multi-value filters, near-duplicate metrics, ratios, time expressions, a segment
metric, out-of-scope questions and misspellings. Each answerable case has a hand-written gold query,
any equivalent alternatives, and its frozen answer. Answers compare as sets of rows whose columns
are named by what they hold (a measure or metric, a dimension, or the time bucket), so aliases and
column order don't matter but a value in the wrong column does. A dimension pinned to one value by
a filter is left out, as it adds only a constant column. Numbers match within a relative tolerance
of 1e-8, row order counts only for rankings, and time buckets count only for trends.

A held-out split of 12 more questions is kept outside the repository so the planner can't be tuned
against it. `HELDOUT_SET_SHA256` in the script commits to its content. `--eval-file` scores a file
only when it matches the dev or held-out digest, unless `--allow-unfrozen` is passed.

## Production Readiness

The packaged MCP server is suitable for local agents and trusted service wrappers that need stable
tool/resource/prompt definitions, structured JSON-RPC errors, request IDs, and optional API-key
protection. Process supervision, network TLS, host-level authorization, tenant isolation, and
secret rotation remain deployment responsibilities rather than MCP protocol logic.
