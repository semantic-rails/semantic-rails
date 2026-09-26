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

- Runtime, compile-cache, HTTP service, MCP adapter, and JSON-RPC dispatcher.
- Request context, trusted attributes, and the resolver and audit-sink
  protocols/setters.
- Warehouse adapter base/concrete classes, adapter factory, connection-option
  normalization, connector registry functions, and option constants.
- Package reference, parse/validation, and package-test services.

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
