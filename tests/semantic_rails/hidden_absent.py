"""The test-side oracle for objects hidden from a caller: the same package with them absent.

``absent(config, hidden)`` deletes the hidden rows, trims and drops what names them (policies,
``path_preferences``, aggregate relations, caveats) and drops the visibility policies. Authored
text that names a hidden object is left out as the authoring guide states: a text field reads
"", a list or mapping loses the items naming one, a name is rebuilt from the id, a caveat is
dropped. Every field not in ``ENGINE_READ`` is authored. It is built from the package's own
dataclasses, independently of the engine's visible view. A response to a caller with ``hidden`` objects is compared, whole, with
the response from the absent package, and searched for every token of the hidden objects: each
id, and each name, label or alias that no visible object shares.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import fields, is_dataclass, replace
from typing import Any

from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.request_context import context_from_policy_context
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PackageConfig, SemanticPolicyConfig

CALLER = {"roles": ["support"]}
ELIGIBLE = {"roles": ["finance"]}
ACTIONS = ("hidden", "visible_only")
OBJECT_FIELDS = (
    "entities",
    "dimensions",
    "temporal_roles",
    "relationships",
    "value_domains",
    "measures",
    "metric_recipes",
    "segments",
)
# Per-call values (ids, timings, whether this runtime compiled the query before), and the
# identity of the package build: two runtimes over different packages report different
# fingerprints by construction, whatever the caller sees.
VOLATILE = frozenset(
    {
        "request_id",
        "timing_ms",
        "cache_lookup_ms",
        "compile_ms",
        "cache_hit",
        "semantic_fingerprint",
        "source_fingerprint",
    }
)
# Fields the engine reads: ids and references, physical names, enums and executable specs.
# Every other text, list or mapping field of a package record is authored; this list is the
# test's own, written independently of the engine's projection table.
ENGINE_READ = frozenset(
    {
        *("id", "package_id", "kind", "value", "expr", "expression", "steps", "accumulation"),
        *("table", "primary_key", "relation_id", "key", "identifiers", "foreign_keys"),
        *("calendar_id", "entity", "column", "data_type", "value_domain", "dimension"),
        *("temporal_class", "supported_grains", "timezone", "column_timezone"),
        *("source_entity", "target_entity", "source_column", "target_column", "cardinality"),
        *("safety", "source_columns", "target_columns", "source_key_role", "target_key_role"),
        *("allowed_directions", "target_key_type", "rollup_safe_aggregations_reverse"),
        *("dimensions", "from_", "to", "row_grain", "default_aggregation", "value_type"),
        *("allowed_aggregations", "source_relation", "invalid_aggregations", "measure_class"),
        *("compatible_temporal_roles", "suggested_aggregations", "comparison_peers"),
        *("clock_variants", "preferred_companion_metrics", "default_temporal_role"),
        *("cross_window_policy", "lookup_from", "lookup_via", "temporal_role", "filter_spec"),
        *("window_spec", "basis_metric", "preview_dimensions", "where", "metric_filters"),
        *("time", "temporal_role_overrides", "relationship_path", "relation", "measures"),
        *("grain", "entity_grain", "model_id", "variant_id", "source", "time_column"),
        *("eligible_time_grains", "measure_columns", "measure_rollups", "measure_aggregations"),
        *("measure_holds", "dimension_columns", "dimension_paths", "excluded_entities"),
        *("excluded_dimensions", "equivalence_kind", "output_name", "columns", "warehouse"),
        *("default_db", "seed", "connection", "environments", "planner", "observation_scope"),
        *("object_ids", "audiences", "roles", "action", "severity"),
    }
)
# An authored mapping's keys the engine reads: the export hint (a boolean) and a historical
# join's physical columns.
ENGINE_KEYS = {
    "meta": {"mnpi"},
    "operational": {"mnpi"},
    "temporal_validity": {"valid_from", "valid_to"},
}
# metric_constraint config keys that list object ids.
CONSTRAINT_LISTS = (
    "required_group_by",
    "allowed_group_by",
    "allowed_where",
    "allowed_metric_filter_entities",
    "allowed_metric_filter_metrics",
    "allowed_temporal_roles",
)


def visibility_policy(action: str, *object_ids: str) -> SemanticPolicyConfig:
    """``action`` hides ``object_ids`` from the support caller; finance is eligible."""
    roles = ["finance"] if action == "visible_only" else ["support"]
    return SemanticPolicyConfig(
        id=f"policy.test.{action}",
        kind="object_visibility",
        action=action,
        object_ids=list(object_ids),
        roles=roles,
    )


def access_policy(action: str, *object_ids: str, **changes: Any) -> SemanticPolicyConfig:
    policy = SemanticPolicyConfig(
        id=f"policy.test.{action}",
        kind="object_access",
        action=action,
        object_ids=list(object_ids),
        roles=["support"],
    )
    return replace(policy, **changes)


def object_rows(config: PackageConfig) -> list[Any]:
    return [row for name in OBJECT_FIELDS for row in getattr(config, name)]


def with_policies(config: PackageConfig, *policies: SemanticPolicyConfig) -> PackageConfig:
    return replace(config, semantic_policies=[*config.semantic_policies, *policies])


def references(value: Any, ids: Iterable[str]) -> set[str]:
    """Every string anywhere in ``value`` (dataclass fields, mapping keys and values, list
    items) that equals one of ``ids``."""
    known = set(ids)
    found: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, str):
            if item in known:
                found.add(item)
        elif is_dataclass(item) and not isinstance(item, type):
            for field in fields(item):
                walk(getattr(item, field.name))
        elif isinstance(item, Mapping):
            for key, child in item.items():
                walk(key)
                walk(child)
        elif isinstance(item, list | tuple | set | frozenset):
            for child in item:
                walk(child)

    walk(value)
    return found


def decoded_strings(value: Any) -> Iterator[str]:
    """Every string in ``value`` as it reads, never serialized (JSON escapes non-ASCII text
    and quotes): dataclass fields, mapping keys and values, list items."""
    if isinstance(value, str):
        yield value
    elif is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            yield from decoded_strings(getattr(value, field.name))
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield from decoded_strings(key)
            yield from decoded_strings(child)
    elif isinstance(value, list | tuple | set | frozenset):
        for child in value:
            yield from decoded_strings(child)


def declared_references(config: PackageConfig) -> dict[str, set[str]]:
    """Each object's declared references to other objects: any field value equal to an id."""
    ids = {row.id for row in object_rows(config)}
    return {
        row.id: references([getattr(row, f.name) for f in fields(row) if f.name != "id"], ids)
        - {row.id}
        for row in object_rows(config)
    }


def _trimmed(values: Any, hidden: set[str]) -> Any:
    if isinstance(values, list):
        return [item for item in values if not (isinstance(item, str) and item in hidden)]
    return values


def _named(value: Any, tokens: Mapping[str, re.Pattern[str]]) -> bool:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return bool(value) and any(pattern.search(text) for pattern in tokens.values())


def _unnamed(row: Any, tokens: Mapping[str, re.Pattern[str]]) -> Any:
    """``row`` without authored text that names a hidden object: such a text reads "", a
    list or mapping loses the items naming one (a mapping keeps its engine keys), and a name
    is rebuilt from the id's last segment (a value's label from the value)."""
    changes: dict[str, Any] = {}
    for field in fields(row):
        value = getattr(row, field.name)
        if field.name in ENGINE_READ or not value:
            continue
        items = value if isinstance(value, list) else [value]
        if all(is_dataclass(item) for item in items):
            shown: Any = [_unnamed(item, tokens) for item in items]
            shown = shown if isinstance(value, list) else shown[0]
        elif isinstance(value, Mapping):
            keep = ENGINE_KEYS.get(field.name, set())
            shown = {
                key: bool(item) if key == "mnpi" else item
                for key, item in value.items()
                if key in keep or not _named([key, item], tokens)
            }
        elif isinstance(value, list):
            shown = [item for item in value if not _named(item, tokens)]
        elif not _named(value, tokens):
            continue
        elif field.name == "label" and hasattr(row, "value"):
            shown = str(row.value)
        elif field.name == "name":
            shown = str(getattr(row, "id", "") or getattr(row, "package_id", "")).split(".")[-1]
        else:
            shown = ""
        if shown != value:
            changes[field.name] = shown
    return replace(row, **changes) if changes else row


def absent(config: PackageConfig, hidden: Iterable[str]) -> PackageConfig:
    """``config`` with every hidden object absent, and nothing left naming one."""
    gone = set(hidden)
    tokens = hidden_tokens(config, gone)
    rows = {
        name: [_unnamed(r, tokens) for r in getattr(config, name) if r.id not in gone]
        for name in OBJECT_FIELDS
    }
    policies = []
    for policy in config.semantic_policies:
        if policy.kind == "object_visibility":
            continue
        listed = [object_id for object_id in policy.object_ids if object_id not in gone]
        if policy.object_ids and not listed:
            continue
        settings = {
            key: _trimmed(value, gone) if key in CONSTRAINT_LISTS else value
            for key, value in policy.config.items()
        }
        settings = {
            key: value for key, value in settings.items() if not _named([key, value], tokens)
        }
        if policy.kind == "row_filter" and settings.get("dimension") in gone:
            continue
        policies.append(replace(policy, object_ids=listed, config=settings))
    caveats = []
    for caveat in config.semantic_caveats:
        listed = [object_id for object_id in caveat.object_ids if object_id not in gone]
        if caveat.object_ids and not listed:
            continue
        rest = [caveat.entity_values, caveat.time, caveat.references, caveat.config]
        if references(rest, gone) or _named(
            [caveat.id, caveat.message, caveat.owner, rest], tokens
        ):
            continue
        caveats.append(replace(caveat, object_ids=listed))
    return replace(
        config,
        **rows,
        semantic_policies=policies,
        semantic_caveats=caveats,
        path_preferences=[
            _unnamed(row, tokens) for row in config.path_preferences if not references(row, gone)
        ],
        aggregate_relations=[
            _unnamed(row, tokens) for row in config.aggregate_relations if not references(row, gone)
        ],
        relations=[_unnamed(row, tokens) for row in config.relations],
        package=_unnamed(config.package, tokens),
        operational_contract={}
        if _named(config.operational_contract, tokens)
        else config.operational_contract,
        meta_contract={} if _named(config.meta_contract, tokens) else config.meta_contract,
    )


def envelope(response: Any) -> Any:
    """``response`` without per-call values (request ids, timings) or package fingerprints."""
    if isinstance(response, dict):
        return {key: envelope(value) for key, value in response.items() if key not in VOLATILE}
    if isinstance(response, list):
        return [envelope(item) for item in response]
    return response


def _identities(row: Any) -> list[str]:
    return [
        value for value in (row.id, getattr(row, "name", ""), getattr(row, "label", "")) if value
    ] + list(getattr(row, "aliases", []) or [])


def _word(text: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(text) + r"(?![A-Za-z0-9_])", re.IGNORECASE)


def hidden_tokens(config: PackageConfig, hidden: Iterable[str]) -> dict[str, re.Pattern[str]]:
    """Each hidden id, and each hidden name, label or alias no visible object's id, name, label
    or alias contains as a word."""
    gone = set(hidden)
    visible = " \n ".join(
        text for row in object_rows(config) if row.id not in gone for text in _identities(row)
    )
    tokens: dict[str, re.Pattern[str]] = {}
    for row in object_rows(config):
        if row.id not in gone:
            continue
        tokens[row.id] = re.compile(re.escape(row.id) + r"(?![A-Za-z0-9_])")
        for text in _identities(row)[1:]:
            text = text.strip()
            if len(text) > 1 and not _word(text).search(visible):
                tokens[text] = _word(text)
    return tokens


def leaks(
    response: Any, tokens: Mapping[str, re.Pattern[str]], *, request: Any = None
) -> list[str]:
    """The tokens ``response`` contains, other than ones the caller's own request carries."""
    text = json.dumps(response, sort_keys=True, default=str)
    asked = json.dumps(request, sort_keys=True, default=str) if request is not None else ""
    return sorted(
        token
        for token, pattern in tokens.items()
        if pattern.search(text) and not pattern.search(asked)
    )


def mcp_call(runtime: Runtime, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return SemanticLayerMCPAdapter(runtime).call_tool(tool, arguments)


def http_call(
    runtime: Runtime, route: str, body: dict[str, Any], context: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    service = SemanticHTTPService(runtime)
    try:
        response, status = service.handle("POST", route, body)
    except SemanticLayerError as exc:
        caller = context_from_policy_context(context or body.get("policy_context") or {})
        response, status = service.exception_payload(exc, stage="http", context=caller)
    return {**response, "http_status": status}


def outcome(call: Callable[[], Any]) -> Any:
    """A response, or the raised refusal as code, message and details."""
    try:
        return call()
    except SemanticLayerError as exc:
        return {"raised": exc.code, "message": str(exc), "details": exc.details}
