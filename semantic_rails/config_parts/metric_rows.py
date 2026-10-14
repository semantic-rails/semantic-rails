"""Authored metric fields and expression reference resolution."""

from __future__ import annotations

from typing import Any

from ..errors import SemanticLayerError
from ..expressions import parse_semantic_expression

# Direct metric fields that shape the expression. Every one an author writes reaches the
# expression, where the parser keeps it (it names a field the kind supports) or rejects it
# (it does not), so no field is dropped between the YAML and the compiled metric.
_DIRECT_EXPRESSION_FIELDS = (
    "measure",
    "aggregation",
    "numerator",
    "denominator",
    "window",
    "window_scope",
    "offset",
    "period",
    "partition_by",
    "order_by",
)
_WINDOWED_DIRECT_KINDS = frozenset({"cumulative", "rolling", "prior_period", "period_to_date"})


def _authored_direct_fields(spec: dict[str, Any], *, consumed: set[str]) -> dict[str, Any]:
    """The direct fields the author wrote that the translator did not already place."""
    return {
        field: spec[field]
        for field in _DIRECT_EXPRESSION_FIELDS
        if field not in consumed and spec.get(field) is not None
    }


def _translate_metric_direct_fields(
    spec: dict[str, Any],
    *,
    resolve,
    resolve_metric=None,
    resolve_dimension=None,
    known_dimensions: frozenset[str] | set[str] = frozenset(),
    classify_ref=None,
    context: str = "",
    suggest=None,
) -> dict[str, Any]:
    """Translate direct named fields per metric kind into the runtime
    expression AST shape.

    Common kinds — `aggregate`, `ratio`, `cumulative`, `rolling`,
    `prior_period`, `period_to_date` — get direct named fields:
        kind: aggregate     → measure: <key>
        kind: ratio         → numerator: <key>, denominator: <key>
        kind: cumulative    → measure: <key>, optional partition_by
        kind: rolling       → measure: <key>, window: { ... }, optional partition_by
        kind: prior_period  → measure: <key>, offset: { ... }
        kind: period_to_date→ measure: <key>, period: <str>, optional partition_by

    Any other direct field written on a metric (a `window` on a cumulative, a
    `partition_by` on a ratio) is carried into the expression as well; the
    expression parser rejects a field its kind does not support.

    Long-tail kinds (`derived`, `conversion`, anything using metric_predicate)
    keep authoring the AST directly under `expression:`. References to other
    metrics/measures use package-relative keys; the loader resolves them via
    the supplied `resolve` callable (measure index) and `resolve_metric`
    callable (metric index, with measure fallback for auto-published metrics).
    Falls back to measure-only resolution when `resolve_metric` is None.
    """
    if not isinstance(spec, dict):
        return spec
    translated = _translate_direct_fields_to_expression(
        spec,
        resolve=resolve,
        resolve_metric=resolve_metric or resolve,
        resolve_dimension=resolve_dimension,
        classify_ref=classify_ref,
        context=context,
        suggest=suggest,
    )
    # The one place the loaded expression is checked, whichever way it was written.
    if translated.get("expression"):
        _resolve_partition_by(
            translated["expression"],
            resolve_dimension=resolve_dimension,
            known_dimensions=known_dimensions,
            context=context,
        )
    return translated


def _translate_direct_fields_to_expression(
    spec: dict[str, Any],
    *,
    resolve,
    resolve_metric,
    resolve_dimension,
    classify_ref,
    context: str,
    suggest,
) -> dict[str, Any]:
    spec = dict(spec)
    if "expression" in spec and spec["expression"]:
        # An `expression:` block is the whole definition. A direct field beside it
        # would be dropped or would contradict it, so the metric is refused.
        stray = sorted(_authored_direct_fields(spec, consumed=set()))
        if stray:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{context}: has an expression: block and also the direct field(s) "
                f"{', '.join(stray)}; write these inside expression:, or drop expression: "
                "and use direct fields only",
                details={"conflicting_fields": stray},
            )
        # Author wrote the AST directly. Pass through, but resolve any
        # package-relative refs inside (best-effort).
        spec["expression"] = _resolve_refs_in_ast(
            spec["expression"],
            resolve=resolve,
            resolve_metric=resolve_metric,
            resolve_dimension=resolve_dimension,
        )
        return spec
    kind = str(spec.get("kind", "")).strip().lower()

    if kind in {"aggregate", "semi_additive"}:
        measure_ref = spec.get("measure")
        if measure_ref is None:
            return spec
        spec["expression"] = {
            "kind": kind,
            "measure": resolve(measure_ref),
            "aggregation": str(spec.get("aggregation", "")),
            **_authored_direct_fields(spec, consumed={"measure", "aggregation"}),
        }
        return spec

    if kind == "ratio":
        numerator = spec.get("numerator")
        denominator = spec.get("denominator")
        if numerator is None or denominator is None:
            return spec

        def _ratio_operand(ref: Any, field: str = "operand") -> dict[str, Any]:
            """Wrap a ratio numerator/denominator as a metric ref when it
            resolves to a top-level metric, or as a measure aggregate when
            it resolves to a measure. With auto-publish gone, ratio
            references must point at something that exists; synthesizing a
            metric ID from a measure (the legacy `measure.X.Y → metric.X.Y`
            convention) would produce a dangling reference.
            """
            text = str(ref or "").strip()
            if not text:
                return {"kind": "metric", "metric": text}
            if text.startswith("metric."):
                return {"kind": "metric", "metric": text}
            if text.startswith("measure."):
                return {"kind": "aggregate", "measure": text, "aggregation": ""}
            # When we know the ref classifies (caller passed classify_ref),
            # prefer measure expansion for measure-only keys so auto-publish
            # is not silently re-introduced.
            if classify_ref is not None:
                kind_hit = classify_ref(text)
                if kind_hit == "measure":
                    measure_id = resolve(text)
                    return {"kind": "aggregate", "measure": measure_id, "aggregation": ""}
                if kind_hit == "metric":
                    return {"kind": "metric", "metric": resolve_metric(text)}
            metric_id = resolve_metric(text)
            if metric_id and metric_id.startswith("metric."):
                return {"kind": "metric", "metric": metric_id}
            measure_id = resolve(text)
            if measure_id and measure_id.startswith("measure."):
                return {"kind": "aggregate", "measure": measure_id, "aggregation": ""}
            # Unresolved ref. With authoring context available, fail here —
            # the author gets the metric, the field, and did-you-mean
            # candidates instead of a late "Unknown metric recipe" from the
            # compiler with no location.
            if context:
                suggestions = list(suggest(text) or []) if suggest is not None else []
                hint = f"; closest matches: {', '.join(suggestions)}" if suggestions else ""
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{context}: {field} '{text}' does not match any measure or "
                    f"metric in this package{hint}",
                )
            # Fall through: leave the resolved metric id (likely synthetic)
            # so downstream validation produces a clear OBJECT_NOT_FOUND.
            return {"kind": "metric", "metric": metric_id or text}

        spec["expression"] = {
            "kind": "arithmetic",
            "op": "divide",
            "left": _ratio_operand(numerator, field="numerator"),
            "right": _ratio_operand(denominator, field="denominator"),
            **_authored_direct_fields(spec, consumed={"numerator", "denominator"}),
        }
        return spec

    if kind in _WINDOWED_DIRECT_KINDS:
        measure_ref = spec.get("measure")
        if measure_ref is None:
            return spec
        # The windowed kind wraps an aggregate over the measure; every other direct
        # field the author wrote goes on the wrapper for the expression parser to
        # keep or reject.
        spec["expression"] = {
            "kind": kind,
            "input": {
                "kind": "aggregate",
                "measure": resolve(measure_ref),
                "aggregation": str(spec.get("aggregation", "")),
            },
            **_authored_direct_fields(spec, consumed={"measure", "aggregation"}),
        }
        return spec

    return spec


_MEASURE_REF_KINDS = frozenset({"aggregate", "semi_additive", "scoped_aggregate", "measure"})


def _resolve_refs_in_ast(node: Any, *, resolve, resolve_metric, resolve_dimension=None) -> Any:
    """Best-effort recursive resolution of package-relative refs inside
    an authored expression AST. Walks `kind: metric` nodes and the nodes that
    name a measure (`aggregate`, `scoped_aggregate`, ...) and rewrites the
    `metric:` / `measure:` field via the appropriate resolver.

    `resolve` resolves measure refs (returns measure.<ns>.<key>).
    `resolve_metric` resolves metric refs. `resolve_dimension(measure_id, key)`
    resolves a short `scoped_aggregate` `where[].field` key against the
    measure's model; a field it does not know is left for query-time lookup.
    """

    def recurse(child: Any) -> Any:
        return _resolve_refs_in_ast(
            child,
            resolve=resolve,
            resolve_metric=resolve_metric,
            resolve_dimension=resolve_dimension,
        )

    if isinstance(node, list):
        return [recurse(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = dict(node)
    kind = str(out.get("kind", "")).strip().lower()
    if kind == "metric" and "metric" in out:
        out["metric"] = resolve_metric(out["metric"])
    if kind in _MEASURE_REF_KINDS and "measure" in out:
        out["measure"] = resolve(out["measure"])
    # The prior_period shorthand names its measure directly, like an aggregate does.
    if kind == "prior_period" and isinstance(out.get("measure"), str):
        out["measure"] = resolve(out["measure"])
    if kind == "scoped_aggregate" and resolve_dimension is not None:
        where = out.get("where")
        if isinstance(where, list):
            out["where"] = [
                {**item, "field": resolve_dimension(str(out.get("measure", "")), item["field"])}
                if isinstance(item, dict) and isinstance(item.get("field"), str)
                else item
                for item in where
            ]
    # A bare measure ref (no kind, just {measure: <key>}) appears in
    # metric_predicate inputs and conversion base/converted shorthands.
    # Resolve the key here so package-relative refs survive into runtime.
    if not kind and "measure" in out and "metric" not in out:
        out["measure"] = resolve(out["measure"])
    for key, value in list(out.items()):
        if isinstance(value, (dict, list)):
            out[key] = recurse(value)
    return out


_PARTITIONED_KINDS = frozenset({"cumulative", "rolling", "period_to_date"})


def _input_measure(node: dict[str, Any]) -> str:
    """The measure a windowed node reads, when its input is (or wraps) one measure."""
    inner = node.get("input")
    while isinstance(inner, dict):
        if isinstance(inner.get("measure"), str):
            return str(inner["measure"])
        inner = inner.get("input")
    return ""


def _resolve_partition_by(
    node: Any, *, resolve_dimension, known_dimensions: frozenset[str] | set[str], context: str
) -> None:
    """Rewrite every `partition_by` entry to a known dimension id, or refuse.

    A window partitions by a dimension the query groups by. An entry that names no
    dimension of the package would only surface as a SQL error at query time, so it is
    refused here. A short key resolves against the model of the measure the window
    reads; when that is not one measure, only a full dimension id resolves.
    """
    if isinstance(node, list):
        for item in node:
            _resolve_partition_by(
                item,
                resolve_dimension=resolve_dimension,
                known_dimensions=known_dimensions,
                context=context,
            )
        return
    if not isinstance(node, dict):
        return
    if (
        str(node.get("kind", "")).strip().lower() in _PARTITIONED_KINDS
        and "partition_by" in node
        and node["partition_by"] not in (None, [])
    ):
        entries = node["partition_by"]
        measure_id = _input_measure(node)
        resolved: list[str] = []
        unknown: list[str] = []
        if not isinstance(entries, list):
            # Never read a bare string as a list of characters.
            unknown.append(str(entries))
        for entry in entries if isinstance(entries, list) else []:
            text = entry.strip() if isinstance(entry, str) else ""
            dimension_id = (
                resolve_dimension(measure_id, text) if resolve_dimension is not None else text
            )
            if dimension_id in known_dimensions:
                resolved.append(dimension_id)
            else:
                unknown.append(str(entry))
        if unknown:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{context}: partition_by must be a list of dimensions of this package; "
                f"unknown: {', '.join(unknown)}",
                details={"unknown_partition_by": unknown},
            )
        node["partition_by"] = resolved
    for value in node.values():
        _resolve_partition_by(
            value,
            resolve_dimension=resolve_dimension,
            known_dimensions=known_dimensions,
            context=context,
        )


def _parse_metric_expression(raw: Any, *, context: str) -> Any:
    """Parse an authored metric expression, naming the metric on any refusal.

    The parser owns which kinds and fields an expression supports; this only adds
    where the author wrote it, keeping the parser's error code and details.
    """
    try:
        return parse_semantic_expression(raw, context="config")
    except SemanticLayerError as exc:
        raise SemanticLayerError(exc.code, f"{context}: {exc}", details=exc.details) from exc
    except (TypeError, ValueError) as exc:
        # A part the parser reads as an object or a number that is neither
        # (`window: 7`, `parameters: 5`, `window.value: "7d"`).
        raise SemanticLayerError(
            "INVALID_EXPRESSION_AST", f"{context}: expression has a malformed value: {exc}"
        ) from exc
