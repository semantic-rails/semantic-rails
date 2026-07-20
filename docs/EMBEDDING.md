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
- Request-context resolver and audit-sink protocols/setters.
- Warehouse adapter base/concrete classes, adapter factory, connection-option
  normalization, connector registry functions, and option constants.
- Package reference, parse/validation, and package-test services.

A multi-tenant host must derive `RequestContext` from authenticated identity and
pass it to every remote MCP/HTTP request boundary. It must not trust
caller-supplied policy context. Custom warehouse credentials remain a host
concern; the engine receives an adapter through `Runtime.set_adapter`.

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
