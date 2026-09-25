"""Trusted resource grants shared by every governed engine operation.

Metric invocation is distinct from access to its implementation. A granted
recipe may read its dependencies, but callers cannot query those measures or
columns independently. Restricted metadata is an explicit public projection;
the full package remains immutable and private to compilation.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from .ast import normalize_query
from .compiler import bind_metadata_objects, bind_query
from .errors import ERROR_CODES, SemanticLayerError, query_execution_error
from .expressions import MetricRecipeRefExpr, collect_object_references
from .policies import enforce_query_policies
from .request_context import RequestContext, context_from_policy_context
from .schema import PackageConfig


def access_denied() -> SemanticLayerError:
    # Do not distinguish missing resources from ungranted resources or reveal
    # package policy names, dependencies, or nearest-match suggestions.
    return SemanticLayerError("RESOURCE_ACCESS_DENIED", "Resource access is not permitted.")


def _public_error(exc: SemanticLayerError) -> SemanticLayerError:
    if exc.code in {
        "RESOURCE_ACCESS_DENIED",
        "POLICY_DENIED",
        "OBJECT_NOT_FOUND",
        "AMBIGUOUS_ALIAS",
    }:
        return access_denied()
    if exc.code == "QUERY_EXECUTION_ERROR":
        return query_execution_error({})
    # Preserve actionable operational/validation codes; package-generated
    # messages, details, and recovery candidates may name hidden objects.
    code = exc.code if exc.code in ERROR_CODES else "INVALID_QUERY"
    return SemanticLayerError(code, "The requested operation could not be completed.")


def _capabilities(access: ResourceAccess) -> dict[str, Any]:
    from .metadata_parts.capabilities import _EXPRESSION_SHAPES

    available = any(row["kind"] == "metric" for row in access.visible_rows())
    return {
        "package_id": access.config.package.package_id,
        "package": {
            "id": access.config.package.package_id,
            "package_id": access.config.package.package_id,
            "name": access.config.package.name,
        },
        "schema_version": access.config.version,
        "capabilities": [{"kind": "granted_metric_queries", "available": True, "reason": ""}]
        if available
        else [],
        "unsupported_capabilities": [
            {
                "kind": kind,
                "available": False,
                "reason": "Unavailable with restricted metric grants.",
            }
            for kind in (
                "raw_expressions",
                "segments",
                "valid_values",
                "general_intent_composition",
            )
        ],
        "expression_shapes": [
            dict(shape) for shape in _EXPRESSION_SHAPES if shape["name"] == "metric"
        ]
        if available
        else [],
    }


def _references(value: Any, config: PackageConfig | None = None, *, owner: str = "") -> set[str]:
    return set(collect_object_references(value, config, owner=owner))


@dataclass(frozen=True)
class ResourceAccess:
    config: PackageConfig
    context: RequestContext

    @classmethod
    def from_context(
        cls, config: PackageConfig, context: Mapping[str, Any] | None
    ) -> ResourceAccess:
        return cls(config, context_from_policy_context(context))

    @property
    def restricted(self) -> bool:
        return self.context.metric_allowlist is not None

    def _check_policies(self, object_ids: set[str], query: dict[str, Any] | None = None) -> None:
        try:
            references = set(object_ids)
            binding = None
            if query is not None:
                binding = bind_query(self.config, None, query)
                references = set(binding.object_ids)
            else:
                references.update(bind_metadata_objects(self.config, object_ids))
            enforce_query_policies(
                self.config,
                references,
                environment=self.context.environment,
                audience=self.context.audience,
                roles=self.context.roles,
                query=query,
                binding=binding,
            )
        except SemanticLayerError:
            raise access_denied() from None

    def visible_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        collections = (
            ("metric", self.config.metric_recipes, self.context.metric_allowlist or ()),
            ("dimension", self.config.dimensions, self.context.dimension_allowlist or ()),
            ("temporal_role", self.config.temporal_roles, self.context.dimension_allowlist or ()),
        )
        # An empty metric grant cannot expose supporting data resources.
        if not self.context.metric_allowlist:
            return rows
        for kind, objects, allowed in collections:
            for obj in objects:
                if obj.id not in allowed:
                    continue
                try:
                    self._check_policies({obj.id})
                except SemanticLayerError:
                    continue
                rows.append(
                    {
                        "id": obj.id,
                        "kind": kind,
                        "object_type": kind,
                        "name": obj.name,
                        "label": obj.label,
                        "available": True,
                    }
                )
        return rows

    def enforce_query(self, payload: dict[str, Any]) -> None:
        if not self.restricted:
            return
        allowed_keys = {
            "version",
            "select",
            "group_by",
            "where",
            "order_by",
            "time",
            "limit",
            "policy_context",
            "request_id",
            "verbosity",
            "sql_profile",
            "render_profile",
            "limits",
            "debug",
            "explain",
            "export",
        }
        if set(payload) - allowed_keys:
            raise access_denied()
        try:
            query = normalize_query(payload)
        except (SemanticLayerError, TypeError, ValueError, KeyError):
            raise access_denied() from None
        if not query.select:
            raise access_denied()
        metrics = set(self.context.metric_allowlist or ())
        known_metrics = {row.id for row in self.config.metric_recipes}
        references: set[str] = set()
        for selected in query.select:
            # This intentionally bounded contract retains metric provenance.
            # Raw/conditional aggregates and wrappers cannot launder measures.
            if not isinstance(selected.expression, MetricRecipeRefExpr):
                raise access_denied()
            metric = selected.expression.metric_recipe
            if metric not in metrics or metric not in known_metrics:
                raise access_denied()
            references.add(metric)
        dimensions = set(self.context.dimension_allowlist or ())
        known_dimensions = {row.id for row in self.config.dimensions}
        requested_dimensions = set(query.group_by) | {row.field for row in query.where}
        if not requested_dimensions <= dimensions & known_dimensions:
            raise access_denied()
        references.update(requested_dimensions)
        if query.time is not None:
            if "calendar_id" in dict(payload.get("time") or {}):
                raise access_denied()
            role = query.time.temporal_role
            if role not in dimensions or role not in {row.id for row in self.config.temporal_roles}:
                raise access_denied()
            references.add(role)
        if query.metric_filters or query.temporal_role_overrides:
            raise access_denied()
        output_names = {row.as_ for row in query.select} | requested_dimensions
        if any(row.field not in output_names for row in query.order_by):
            raise access_denied()
        self._check_policies(references, payload)


def _context_payload(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    if name in {"validate", "compile", "query"}:
        payload = args[0] if args else kwargs.get("payload", {})
        return dict(payload or {})
    if "policy_context" in kwargs:
        return {"policy_context": kwargs["policy_context"]}
    return dict(kwargs.get("partial_query", kwargs.get("query")) or {})


_BUCKETS = {
    "metric": "metrics",
    "dimension": "dimensions",
    "temporal_role": "temporal_roles",
    "measure": "measures",
    "entity": "entities",
    "segment": "segments",
    "relationship": "relationships",
    "value_domain": "value_domains",
}


def _catalog(access: ResourceAccess, kwargs: dict[str, Any]) -> dict[str, Any]:
    from .metadata import format_catalog_payload

    rows = access.visible_rows()
    kind, search = kwargs.get("kind", ""), str(kwargs.get("search", "")).lower()
    rows = [
        row
        for row in rows
        if (not kind or row["kind"] == kind)
        and (not search or search in " ".join(str(row[k]) for k in ("id", "name", "label")).lower())
    ]
    grouped = {
        bucket: [row for row in rows if row["kind"] == kind] for kind, bucket in _BUCKETS.items()
    }
    capabilities = _capabilities(access)
    return format_catalog_payload(
        grouped,
        package=capabilities["package"],
        schema_version=access.config.version,
        view=kwargs.get("view", "summary"),
        verbosity=kwargs.get("verbosity", "compact"),
        supported_capabilities=capabilities["capabilities"],
        unsupported_capabilities=capabilities["unsupported_capabilities"],
    )


def _discover(access: ResourceAccess, kwargs: dict[str, Any]) -> dict[str, Any]:
    terms = set(re.findall(r"[a-z0-9]+", str(kwargs.get("terms", "")).lower()))
    kinds = kwargs.get("kinds") or ()
    rows = []
    for row in access.visible_rows():
        if kinds and row["kind"] not in kinds:
            continue
        tokens = set(
            re.findall(r"[a-z0-9]+", " ".join(str(row[k]) for k in ("id", "name", "label")).lower())
        )
        overlap = terms & tokens
        if terms and not overlap:
            continue
        starter = (
            {"select": [{"expression": {"metric": row["id"]}, "as": "value"}]}
            if row["kind"] == "metric"
            else {"group_by": [row["id"]]}
            if row["kind"] == "dimension"
            else {}
        )
        rows.append(
            {**row, "score": len(overlap), "match_reasons": [], "starter_query_patch": starter}
        )
    rows.sort(key=lambda row: (-row["score"], row["id"]))
    limit = max(1, int(kwargs.get("limit", 10)))
    return {
        **{
            bucket: [row for row in rows if row["kind"] == kind][:limit]
            for kind, bucket in _BUCKETS.items()
        },
        "blocked": [],
        "dimension_values": [],
        "selection_context": {},
        "query_state": {},
    }


def _restricted_plan(
    runtime: Any, access: ResourceAccess, kwargs: dict[str, Any]
) -> dict[str, Any]:
    intent = kwargs.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        raise access_denied()
    partial = dict(kwargs.get("partial_query") or {})
    partial.pop("policy_context", None)
    partial.pop("request_context", None)
    partial.pop("request_id", None)
    # Restrict inference to exact, visible names/labels/IDs. More complex
    # composition stays unavailable until it retains invocation provenance.
    lowered = intent.lower()
    rows = access.visible_rows()
    matches = [
        row
        for row in rows
        if row["kind"] == "metric"
        and any(str(row[key]).lower() in lowered for key in ("id", "name", "label") if row[key])
    ]
    matches.sort(key=lambda row: (-max(len(str(row[k])) for k in ("name", "label")), row["id"]))
    base: dict[str, Any] = {
        "plan_version": 1,
        "status": "unrealizable",
        "best": None,
        "next": {},
        "intent_ir": {},
    }
    if not matches:
        return {
            **base,
            "why": {
                "code": "NO_AUTHORIZED_PLAN",
                "message": "Choose a visible metric and optional granted dimensions.",
            },
        }
    row = matches[0]
    query = {"version": 1, **partial}
    query.setdefault("select", [{"expression": {"metric": row["id"]}, "as": "value"}])
    dimensions = [
        candidate["id"]
        for candidate in rows
        if candidate["kind"] == "dimension"
        and any(
            str(candidate[key]).lower() in lowered
            for key in ("id", "name", "label")
            if candidate[key]
        )
    ]
    remainder = lowered
    for candidate in [row, *[item for item in rows if item["id"] in dimensions]]:
        for phrase in sorted(
            (str(candidate[key]).lower() for key in ("id", "name", "label") if candidate[key]),
            key=len,
            reverse=True,
        ):
            remainder = remainder.replace(phrase, " ")
    filler = {"show", "me", "the", "by", "per", "for", "with", "and", "please", "get", "calculate"}
    if set(re.findall(r"[a-z0-9]+", remainder)) - filler:
        return {
            **base,
            "why": {
                "code": "NO_AUTHORIZED_PLAN",
                "message": "This intent requires unsupported composition; choose an explicit visible metric and granted dimensions.",
            },
        }
    if dimensions:
        query.setdefault("group_by", dimensions)
    if query["select"] != [{"expression": {"metric": row["id"]}, "as": "value"}]:
        try:
            selected = normalize_query(query).select
        except SemanticLayerError:
            raise access_denied() from None
        if (
            len(selected) != 1
            or not isinstance(selected[0].expression, MetricRecipeRefExpr)
            or selected[0].expression.metric_recipe != row["id"]
        ):
            raise access_denied()
    validation = runtime.validate({**query, "policy_context": access.context.to_policy_context()})
    if not validation.get("ok"):
        return {
            **base,
            "why": {"code": "NO_AUTHORIZED_PLAN", "message": "The requested plan is unavailable."},
        }
    best = {
        "query_ir": query,
        "validation_ok": True,
        "pattern": "granted_metric",
        "resolved": [row],
        "rationale": ["Uses an explicitly granted metric."],
    }
    return {
        **base,
        "status": "ok",
        "best": best,
        "next": {"validate": {"query": query}, "ready_for": ["execute"]},
    }


def run_authorized_operation(
    operation: Callable[..., Any], runtime: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """The existing runtime request boundary calls this for shared operations."""
    name = operation.__name__
    payload = _context_payload(name, args, kwargs)
    if (payload.get("policy_context") or {}).get("metric_allowlist") is None:
        return operation(runtime, *args, **kwargs)
    access = ResourceAccess.from_context(runtime._config, payload.get("policy_context"))
    try:
        if name in {"validate", "compile", "query"}:
            access.enforce_query(payload)
            result = operation(runtime, *args, **kwargs)
            if not result.get("ok", False):
                first: dict[str, Any] = next(iter(result.get("errors") or []), {})
                raise _public_error(SemanticLayerError(str(first.get("code", "INVALID_QUERY")), ""))
            # Execution metadata can contain dependency IDs, related metrics,
            # policies and diagnostic suggestions. Expose only the result and
            # requested SQL in the bounded resource-grant contract.
            response = {
                key: value
                for key, value in result.items()
                if key
                in {
                    "ok",
                    "status",
                    "rows",
                    "row_count",
                    "truncated",
                    "rendered_sql",
                    "compile_stats",
                    "request_context",
                    "timing_ms",
                    "warehouse",
                    "dialect",
                    "sql_profile",
                }
            }
            from .runtime_parts.responses import output_columns

            # Reuse the engine's descriptor builder even when minimal verbosity
            # omitted it. It uses the authorized query, not expanded recipes.
            columns = output_columns(
                access.config,
                {"explain": SimpleNamespace(normalized_query=normalize_query(payload).to_dict())},
            )
            permitted = set(access.context.metric_allowlist or ()) | set(
                access.context.dimension_allowlist or ()
            )
            response["output_columns"] = [
                {
                    key: value
                    for key, value in column.items()
                    if key in {"field", "semantic_id", "display_label", "sql_alias", "type"}
                }
                for column in columns
                if column.get("semantic_id") in permitted
            ]
            return response
        if name == "capabilities_payload":
            return _capabilities(access)
        if name in {"catalog_payload", "resolve_catalog"}:
            return _catalog(access, kwargs)
        if name == "discover_payload":
            return _discover(access, kwargs)
        if name == "inspect_payload":
            card = next(
                (row for row in access.visible_rows() if row["id"] == kwargs.get("object_id")), None
            )
            if card is None:
                raise access_denied()
            return {"object_id": card["id"], "card": card, "query_state": {}}
        if name == "build_options_payload":
            discovery = _discover(
                access, {"terms": kwargs.get("focus_terms", ""), "limit": kwargs.get("limit", 10)}
            )
            options = [
                {
                    **row,
                    "query_patch": {
                        "select": [{"expression": {"metric": row["id"]}, "as": "value"}]
                    },
                }
                for row in discovery["metrics"]
            ]
            return {
                "recommended": options,
                "available": discovery["dimensions"],
                "blocked": [],
                "query_patches": [row["query_patch"] for row in options],
                "query_state": {},
            }
        if name == "plan_payload":
            return _restricted_plan(runtime, access, kwargs)
        if name in {
            "valid_values_payload",
            "segment_validate",
            "segment_explain",
            "segment_preview",
        }:
            raise access_denied()
        # New operations carrying restricted authority must explicitly choose
        # their access semantics before they can run.
        raise access_denied()
    except SemanticLayerError as exc:
        public = _public_error(exc)
        if name == "validate":
            return {
                "ok": False,
                "status": "error",
                "errors": [
                    {
                        "code": public.code,
                        "message": str(public),
                    }
                ],
                "request_context": access.context.to_public_dict(),
            }
        raise public from None
