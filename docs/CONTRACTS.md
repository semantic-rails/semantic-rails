# Public Contracts and Compatibility

Semantic Rails publishes one installable contract bundle in
`semantic_rails.contracts`. The same bytes are mirrored under `schemas/` for
source-checkout and GitHub Release consumers. The installed
`semantic_rails.contracts` bundle and the checksummed GitHub Release assets are
authoritative. `https://semantic-rails.com/schemas/` is a public HTTP mirror
of those exact released bytes, not an independently edited registry.

| Artifact | Owner | Stability |
|---|---|---|
| `architect_mcp.v1.json` | Engine | Stable Architect tools, schemas, annotations, and transaction semantics |
| `package.v1.json` | Engine | Stable project/package major |
| `query_ir.v1.json` | Engine | Stable Query IR v1 |
| `query_ir.preview.v2.json` | Engine | Preview; may change before v2 |
| `semantic_contract.v1.json` | Engine | Stable framework-neutral validation payload |
| `validation_report.v1.json` | Engine | Stable cross-validator report envelope |
| `http_api.v1.openapi.json` | Engine | Stable `/api/v1` operations and envelope |
| `query_mcp.v1.json` | Engine | Stable query-MCP tools, resources, prompts, schemas, and annotations |

The Python distribution version, package schema, Query IR, HTTP API, query MCP,
semantic contract, and validation-report format are separate version
identities. A release may add behavior without advancing every contract.

## Semantic validation ownership

The engine is the sole producer of the framework-neutral `semantic` section:

```python
from semantic_rails.contracts import export_semantic_contract

contract = export_semantic_contract("./my_semantic_project")
```

The equivalent CLI is:

```bash
semantic-rails export-contract --path ./my_semantic_project \
  --output semantic-rails-contract.json
```

The real package loader validates and normalizes the project before export.
`semantic_hash` is SHA-256 over canonical JSON of the loaded semantic config,
excluding connection, seed, and local database locators. It changes with
semantic expressions, models, relationships, policies, and other governed
behavior, but not when files are rearranged or deployment-only paths change.

dbt, SQLMesh, and future integrations own an optional `binding` object. A
binding schema composes with
`https://semantic-rails.com/schemas/semantic_contract.v1.json` and narrows the
extension for its framework. An integration must not independently parse
Semantic Rails YAML or redefine `semantic_hash`.

## Compatibility rules

Within a stable major:

- New optional fields, tools, routes, and issue codes are additive.
- Existing field, tool, route, enum, default, and issue-code meanings do not
  change silently.
- Removing a field/tool/route, narrowing an enum, adding a required field, or
  changing semantics requires a new contract major.
- Readers ignore unknown optional fields. Writers emit only fields declared by
  the selected contract major.
- Deprecation starts with an additive release. Consumers gain dual-read support
  before a provider stops emitting the old shape.
- Error and warning code strings are contract data. New codes are additive;
  reusing a code for a different condition is breaking.

Run the deterministic drift gate:

```bash
python scripts/generate_contract_artifacts.py --check
python scripts/check_contract_compatibility.py \
  --baseline path/to/previous-release/contracts
```

The compatibility checker is deliberately conservative. Contract owners review
any flagged change and either preserve compatibility or introduce a new major.

## Concurrent cross-repository changes

Cross-repository work follows provider-before-removal ordering:

1. Change the engine contract artifact and fixtures first. Classify the change
   as `none`, `additive`, or `breaking`.
2. Generate artifacts and run compatibility checks in the engine PR.
3. Test public bindings against the exact candidate wheel and contract bundle.
4. Release dual-reading binding versions before the engine emits a newly
   preferred shape by default.
5. Build and publish the engine wheel once. Promote those exact verified bytes;
   do not rebuild per consumer.
6. Record engine version, artifact digests, and supported contract majors in
   every consumer release.

Private embedders run their own compatibility workflow against the same
candidate wheel after a trusted merge. Public pull requests never receive
private credentials, and the public engine remains independently buildable and
releasable.

Contract paths, generator/release workflows, and the embedding facade should
have explicit code owners and human approval. One automated change should not
silently redefine the canonical contract and every consumer at once.
