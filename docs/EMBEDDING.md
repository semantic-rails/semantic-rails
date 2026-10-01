# Embedding the Engine

Hosts import supported integration seams from `semantic_rails.embedding`.
Implementation modules remain available to the engine itself, but are not the
cross-repository compatibility boundary.

```python
from semantic_rails.embedding import (
    RequestContext,
    Runtime,
    SemanticHTTPService,
    SemanticLayerMCPAdapter,
    handle_jsonrpc_message,
)

runtime = Runtime.from_path("./my_semantic_project")
service = SemanticHTTPService(runtime)
adapter = SemanticLayerMCPAdapter(runtime)

trusted = RequestContext(
    request_id="request-123",
    actor="authenticated-user-id",
    tenant="tenant-id",
    roles=("analyst",),
    environment="production",
)
response = handle_jsonrpc_message(
    adapter,
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    },
    request_context=trusted,
)
```

The facade includes:

- Runtime, compile-cache, HTTP service, MCP adapter, the `MCPAdapter` protocol, the
  JSON-RPC dispatcher, and the Streamable HTTP handler.
- Request context, trusted attributes, and the resolver and audit-sink
  protocols/setters.
- Warehouse adapter base/concrete classes, adapter factory, connection-option
  normalization, connector registry functions, and option constants.
- Package reference, parse/validation, and package-test services.
- Package authoring: `ArchitectProject`, `ArchitectMutation`, `project_revision`,
  `ABSENT_PROJECT_REVISION`, and `impact_report`.

A multi-tenant host must derive `RequestContext` from authenticated identity and
pass it to every remote MCP/HTTP request boundary. It must not trust
caller-supplied policy context. Custom warehouse credentials remain a host
concern; the engine receives an adapter through `Runtime.set_adapter`.

The host may also attach typed attributes from verified identity, such as a
customer ID mapped from a token claim:

```python
from semantic_rails.embedding import TrustedAttributes

trusted = RequestContext(
    actor="authenticated-user-id",
    attributes=TrustedAttributes({"customer_id": "c-123", "regions": ["eu"]}),
)
```

Names match `[a-z][a-z0-9_]{0,63}`. A value is a non-empty string, an int or a
bool, or a non-empty list of one of those types; anything else raises. The
engine carries the attributes through its internal calls. Request bodies,
headers and plans can't create or replace them (a caller's
`policy_context.attributes` is ignored), and they never appear in the public
`request_context`, echoed queries, errors or audit events. The object is opaque
and immutable: read it with `names` and `get(name)`. Attribute values partition
the compile cache and bind to prepared-statement parameters at execution
(DuckDB only; other adapters deny such statements). A package's `row_filter`
policies produce those parameters: they limit a relation's rows to the
attribute's value, and deny the request when it is missing (see
[PACKAGE_AUTHORING.md](PACKAGE_AUTHORING.md)). A driver error on such a statement
is raised without the driver's message, which could quote a bound value.

For in-memory Snowflake credentials, implement the generic
`ConnectionCredentialProvider` protocol and pass it to the public adapter:

```python
from semantic_rails.embedding import (
    ConnectionCredentialProvider,
    SnowflakeNativeAdapter,
)

adapter = SnowflakeNativeAdapter(
    "tenant-connection",
    options={"database": "ANALYTICS", "warehouse": "COMPUTE_WH"},
    credential_provider=my_provider,
)
```

`credentials_for(warehouse=..., connection_kind=..., connection_name=...)`
returns canonical credential keys without using process environment variables
or files. If the provider retains revealed values, it may implement `clear()`;
`adapter.close()` calls it best-effort and drops the provider reference.

New hosting requirements should first become generic, tested engine seams and
then be added to this facade. Product-specific identity, tenancy, billing,
deployment, and secret-storage code does not belong in the engine.

## Serving MCP over HTTP

`handle_streamable_http_request(adapter, *, method, headers, body=b"", request_context=None)`
answers one stateless MCP Streamable HTTP request and returns an `MCPHTTPResponse`
(`status`, `payload`, `headers`) for the host's web framework to send. Before dispatching
it applies the transport rules: a request with an `Origin` must match
`SEMANTIC_RAILS_CORS_ORIGINS`; only `POST` is served (`OPTIONS` gets 204); the body must
be `application/json` of at most 64 KiB; `Accept` must list both `application/json` and
`text/event-stream`; and an `MCP-Protocol-Version` header must name a supported version.
The function does not authenticate the caller: the host does that first. A multi-tenant
host passes the `request_context` it derived from authenticated identity. Without one, the
process's policy context resolver reads `headers`, and the default resolver trusts
caller-sent identity headers such as `X-Semantic-Tenant` and `X-Semantic-Roles`.

`handle_jsonrpc_message` and `handle_streamable_http_request` accept any object that
satisfies the `MCPAdapter` protocol (`package_id`, `list_tools`, `call_tool`,
`list_resources`, `read_resource`, `list_prompts`, `get_prompt`), so a host can serve its
own tools through the engine's JSON-RPC envelope, errors, and audit events. Optional
`interface` and `instructions` attributes fill the `initialize` result.

To swap the body of one tool, for a test or a host's own implementation, call
`replace_tool_handler(name, handler)` on a `SemanticLayerMCPAdapter`. It accepts only a
tool the adapter lists and changes that adapter instance only. `call_tool` still validates
the arguments, replaces caller-supplied policy context with the trusted one, and emits the
audit event. Like the built-in tools, `handler` runs inside the adapter's response
envelope, so an exception it raises becomes a tool error response. The private
`_tool_handlers` mapping keeps working through the 0.3 series.

A replaced handler takes over the tool's policy enforcement. The metric and dimension
allowlists and the resource-access checks run inside the built-in handlers, not in
`call_tool`. A replacement receives the trusted `policy_context` in its arguments, and the
host is responsible for applying it. To keep the engine's checks, read the built-in with
`adapter.tool_handlers[name]` before replacing it, and call that from your handler.

## Package authoring

`ArchitectProject(project_path, workspace_root=None)` is the transactional authoring
session behind the Architect MCP and the REPL. It edits a package directory on disk:
`revision()`, `inventory()`, `upsert_model`, `upsert_relationship`, `upsert_metric`,
`upsert_segment`, `remove_object`, `write_file`, and `archive_file`. Each change returns an
`ArchitectMutation`: `report` is the result the Architect MCP returns, `changed_files`
lists the files written, and `undo()` restores them unless a later edit changed them.
Hosts receive `ArchitectMutation` objects and never construct them.

- **Containment.** A relative `project_path` resolves against `workspace_root`. The
  project must resolve inside `workspace_root` and contain `package.yml`, symlinked
  project paths are refused, and every write stays inside the project directory. Without
  `workspace_root` the root is the project itself, so a host passes the directory it
  owns. Locks and idempotency receipts are kept under `<workspace_root>/.semantic-rails/`.
- **Concurrency.** Pass `expected_revision` and `idempotency_key` to every change, as the
  Architect MCP does. A change whose base revision is stale fails with `CONFIG_CONFLICT`
  (`conflict_kind: "stale_revision"`), and a retry with the same key and change replays
  the stored result instead of writing again. Without `expected_revision`, a change is
  checked against the revision read when the call starts, which suits one local user.
- **Validation.** Keep the default `validate_after=True`: a change that leaves the package
  unparseable is rolled back. `dry_run=True` reports the change without writing.

`project_revision(project_path)` returns the deterministic revision of a directory's
authored files (`"sha256:…"`), or `ABSENT_PROJECT_REVISION` (`"absent"`) when the directory
is missing or holds no authored files, so a host can check a stored revision without
opening a session.

`impact_report(ref, *, compare_path="", base_ref="", snapshot=None)` compares the package
`ref` names with another package directory (`compare_path`) or with the same package at a
git revision (`base_ref`), and reports the changes, impacted metrics, reviewer teams, and
risk. `base_ref` runs the `git` executable in the repository that holds the package, so a
host serving untrusted callers must not pass it, and must resolve `compare_path` to a
directory it owns rather than take it from the caller.

## Changing the facade

`tests/semantic_rails/test_embedding_consumer_contract.py` checks the facade uses
recorded from a known downstream embedder's code: the names it imports, the attributes it
reads, the argument shapes of the calls whose receiver the scan can place, and the exact
members and parameters of the protocols it implements (the engine calls those). A pull
request that breaks a recorded use fails CI. A method the embedder reaches through an
object the scan can't place is checked for existence only. `uv run python
scripts/embedding_consumer_contract.py --consumer <checkout>` regenerates the list from
the embedder's code; `--check` reports drift without writing.

When the test fails, stage the change across releases instead of making it in one:

1. Add the new form next to the old one. A new parameter gets a default that keeps
   today's behavior, and a moved name stays importable from its old place as the same
   object.
2. Deprecate the old form in a `deprecated` changelog fragment that names the release
   removing it, and pin that release with a test that fails once the version reaches it,
   as `tests/semantic_rails/test_request_context_reexports.py` does.
3. Remove the old form in that release, once the embedder has moved, and regenerate the
   list.

Hosts should test for a capability, such as `hasattr(embedding, "Name")` or a parameter
in `inspect.signature(...)`, rather than compare engine versions.

## Facade reference

Every name `semantic_rails.embedding` exports, with its call shape: parameters in order,
`=` marking one with a default, `/` ending the positional-only ones, `*` starting the
keyword-only ones, and `{…}` listing a protocol's members. `test_embedding_consumer_contract.py` compares this list with the
facade, so a pull request that adds or changes a name updates it too.

```text
ABSENT_PROJECT_REVISION
API_VERSION
ATHENA_CONNECTION_OPTIONS
ArchitectMutation(report, project_path, _snapshots=, _active=)
ArchitectProject(project_path, workspace_root=)
AuditSink{emit(self, payload)}
BIGQUERY_CONNECTION_OPTIONS
CLICKHOUSE_CONNECTION_OPTIONS
CachedCompilation(compiled)
CompiledSqlCache{get(self, key); put(self, key, value)}
ConnectionCredentialProvider{credentials_for(self, *, warehouse, connection_kind, connection_name)}
DATABRICKS_CONNECTION_OPTIONS
DUCKLAKE_CONNECTION_OPTIONS
Database(conn, engine)
DuckDBAdapter(db_path)
HeaderPolicyContextResolver()
LoadedPackageSnapshot(source_path, source_fingerprint, semantic_fingerprint, provenance, source_kind, _config, _authored, _normalized, _semantic)
LruCompiledSqlCache(maxsize=)
MCPAdapter{call_tool(self, name, arguments, /, *, request_context); get_prompt(self, name, arguments, /); list_prompts(self); list_resources(self); list_tools(self); package_id; read_resource(self, uri, /, *, request_context)}
MCPHTTPResponse(status, payload=, headers=)
MCP_PROTOCOL_VERSION
MOTHERDUCK_CONNECTION_OPTIONS
POSTGRES_CONNECTION_OPTIONS
PUBLIC_V1_ROUTES
PackageReference(source_path, package_id=)
PolicyContextResolver{resolve(self, headers, *, payload=, request_id=)}
PreparedQuery(sql, column_mapping=, parameters=)
RequestContext(request_id=, actor=, tenant=, project=, roles=, environment=, audience=, metric_allowlist=, dimension_allowlist=, attributes=)
Runtime(package_id)
SNOWFLAKE_CLI_CONNECTION_OPTIONS
SNOWFLAKE_NATIVE_CONNECTION_OPTIONS
SemanticHTTPService(runtime, package_id=)
SemanticLayerError(code, message, *, details=)
SemanticLayerMCPAdapter(runtime, *, interface=)
SnowflakeCliAdapter(connection_name, options=)
SnowflakeNativeAdapter(connection_name=, options=, *, credential_provider=)
StderrAuditSink()
TrustedAttributes(values=)
WarehouseAdapter()
WarehouseConnectorSpec(name, dialect, connection_kinds=, connection_options=, requires_default_db=, requires_seed=, requires_connection_name=, adapter=)
audit_logging_enabled()
context_from_headers(headers, *, payload=, request_id=)
context_from_policy_context(policy_context, *, request_id=)
create_duckdb_adapter(package, *, db_path=)
create_warehouse_adapter(package, *, db_path=)
dialect_for_warehouse(warehouse)
emit_audit_event(event, **payload)
get_audit_sink()
get_policy_context_resolver()
handle_jsonrpc_message(adapter, message, *, request_context=)
handle_streamable_http_request(adapter, *, method, headers, body=, request_context=)
impact_report(ref, *, compare_path=, base_ref=, snapshot=)
load_package_snapshot(path)
normalize_connection_options(warehouse, kind, options, allowed, *, label=)
parse_config_report(ref, *, progress=)
project_revision(project_path)
request_context_payload(context)
resolve_package_reference(*, package_id=, path=)
run_package_tests_report(ref, *, parse_report=, config=, runtime=)
set_audit_sink(sink)
set_policy_context_resolver(resolver)
supported_warehouses()
validate_config_report(ref, *, progress=, parse_report=, runtime=)
validate_runtime_package(path)
warehouse_connector(warehouse)
```
