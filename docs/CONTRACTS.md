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
| `metric_portability.v1.json` | Engine | Stable read-only metric catalog for BI bindings |
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

## Metric portability for BI consumers

`export_metric_portability(path_or_snapshot)` produces the read-only
`metric_portability.v1.json` projection from the same immutable loaded snapshot
as the validation contract. The existing `export-contract` CLI selects it with
`--format metrics`; the default remains the native validation contract.

A saved BI binding identifies a metric by **package namespace and canonical
metric ID**. Moving or reformatting source files does not change that identity.
Renaming the namespace or ID is an explicit breaking replacement; v1 does not
promise rename stability or globally unique customer namespaces. Hosted apps
must additionally retain their authenticated tenant/package selector.
`semantic_hash`, `definition_hash`, and source fingerprints are change evidence,
never metric IDs or access tokens.

Definitions are projections of the engine's loaded metric and measure models.
The contract references the existing Query IR v1 expression and query schemas;
it does not introduce a second expression language or authorize evaluation
outside the engine. `query_template` requests a metric by reference. Candidate
grouping/filter dimensions and temporal grains describe loaded capabilities;
combinations, warehouse support, policy, and permissions still require runtime
validation. The [small BI card example](../examples/bi_consumer/metric_card.py)
persists the identity and calls an injected authenticated executor.

`compare_metric_portability(before, after)` classifies saved catalog changes:
metric additions are additive; removal/rename, definition changes, capabilities
changes, and engine-version changes require requalification. Display or source
formatting edits are metadata changes. A metric definition hash includes its
transitive metric recipes and a conservative hash of all supporting package
semantics, including measures, relations, policies, and temporal configuration.
Consequently an unrelated supporting-object edit may also require review;
compatibility is not a proof of numerical equivalence. Unknown contract majors,
duplicate IDs, or inconsistent definition hashes fail closed. The comparison ignores
unknown optional top-level fields; a changed field meaning requires a new major and
consumer dual-read qualification before rollout.

Snapshots constructed with `LoadedPackageSnapshot.from_config` contain no
authored namespace; callers must supply `namespace=` explicitly on export.
File-backed snapshots derive it from the captured authored package and reject
an override that would retarget metric identity.

The MetricFlow translator's `TranslationReport.provenance` supplies a versioned
parsed-input digest and loss/unsupported-feature warnings. Pass it explicitly
as `import_provenance` when exporting to preserve that evidence. It records the
input to translation, not a signed claim about subsequent author edits. The
installed `load_contract_fixture("metric_portability.v1.json")` corpus drives
engine and native-adapter conformance without independent fixture copies.
Native dbt macros and SQLMesh graph checks still run without the engine;
translation/export is an optional authoring step outside their runtime.

This full-package author/export artifact contains physical expressions and
supporting definitions. Cloud distribution must use full-package author rights
and immutable package-version identity. A restricted BI principal must use the
grant-aware runtime discovery/query surfaces, not this unfiltered artifact.
No authorization claim in an artifact grants access to data.

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
