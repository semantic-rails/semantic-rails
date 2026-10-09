"""Which metrics govern a measure, read from the metric expressions alone.

A metric governs a measure when it aggregates it through a filter: an aggregate with a
``filter``, or a scoped aggregate with ``where`` or ``predicates`` ("Active stores", the retail
stores of an all-kinds store count). A metric publishes a measure when its whole expression
is that measure's aggregate with no filter, as a ``kind: aggregate`` metric is.

A measure authored with ``publish: false`` that no metric publishes isn't offered by
``discover``; one a metric governs is a building block, and ``plan`` answers with the metrics
that govern it. It stays queryable by id.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from typing import Any

from ..expressions import collect_object_references, expr_to_dict
from ..schema import MetricConfig, PackageConfig
from .route_rows import hop_is_functional

_AGGREGATES = frozenset({"aggregate", "measure", "scoped_aggregate"})
_NARROWING = ("filter", "where", "predicates")


def _measure_reads(node: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    """Each measure an expression aggregates, and that aggregate's narrowing (empty if none)."""

    if isinstance(node, Mapping):
        if node.get("kind") in _AGGREGATES and node.get("measure"):
            yield str(node["measure"]), {key: node[key] for key in _NARROWING if node.get(key)}
            return
        for child in node.values():
            yield from _measure_reads(child)
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from _measure_reads(child)


def whole_aggregate(metric: MetricConfig) -> tuple[str, str, dict[str, Any]] | None:
    """The measure, aggregation and narrowing of a metric that is one measure's aggregate."""

    return _aggregate(expr_to_dict(metric.expression))


def _aggregate(node: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    if node.get("kind") not in _AGGREGATES or not node.get("measure"):
        return None
    if node.get("window") or node.get("anchor"):
        return None
    narrowing = {key: node[key] for key in _NARROWING if node.get(key)}
    return str(node["measure"]), str(node.get("aggregation") or ""), narrowing


def governed_form(metric: MetricConfig) -> tuple[str, str, dict[str, Any]] | None:
    """``whole_aggregate`` of a metric that is one narrowed aggregate, bare or zero-filled.

    A closed list of two shapes: the aggregate itself, or ``COALESCE(<aggregate>, 0)``, which a
    package uses to report an empty count as 0. Any other wrapper, filler or argument is not one.
    """

    node = expr_to_dict(metric.expression)
    args = node.get("args") or []
    if (
        node.get("kind") == "call"
        and str(node.get("name") or "").upper() == "COALESCE"
        and not node.get("distinct")
        and len(args) == 2
        and args[1].get("kind") == "literal"
        and type(args[1].get("value")) in (int, float)
        and args[1]["value"] == 0
    ):
        node = args[0]
    whole = _aggregate(node)
    return whole if whole is not None and whole[2] else None


def published_measure(metric: MetricConfig) -> str:
    """The measure this metric is the plain aggregate of, else ``""``."""

    whole = whole_aggregate(metric)
    return whole[0] if whole is not None and not whole[2] else ""


def governing_metrics(config: PackageConfig, measure_id: str) -> list[MetricConfig]:
    """The metrics that aggregate ``measure_id`` through a filter."""

    return [
        metric
        for metric in config.metric_recipes
        if any(
            read == measure_id and narrowing
            for read, narrowing in _measure_reads(expr_to_dict(metric.expression))
        )
    ]


def _reachable_entities(config: PackageConfig, entity: str) -> set[str]:
    """``entity`` and every entity it reaches over many-to-one or one-to-one relationships."""
    reached, pending = {entity}, [entity]
    while pending:
        current = pending.pop()
        for rel in config.relationships:
            ends = (rel.source_entity, rel.target_entity)
            if current not in ends or not hop_is_functional(rel, current):
                continue
            following = ends[1] if current == ends[0] else ends[0]
            if following not in reached:
                reached.add(following)
                pending.append(following)
    return reached


def population_governors(
    config: PackageConfig, measure_id: str
) -> list[tuple[MetricConfig, frozenset[str]]]:
    """Metrics with narrowing dimensions: any aggregate's filters on the measure's entity, or
    if none, this measure's aggregates' filters on entities reached through functional hops."""
    entity = next((row.entity for row in config.measures if row.id == measure_id), "")
    reached = _reachable_entities(config, entity)
    own = {row.id for row in config.dimensions if row.entity == entity}
    joined = {row.id for row in config.dimensions if row.entity in reached}
    governors = []
    for metric in config.metric_recipes:
        reads = list(_measure_reads(expr_to_dict(metric.expression)))
        mine = [narrowing for read, narrowing in reads if read == measure_id]
        own_hits = own.intersection(
            collect_object_references([narrowing for _, narrowing in reads], config)
        )
        joined_hits = joined.intersection(collect_object_references(mine, config))
        narrowed_by = own_hits or joined_hits
        if narrowed_by:
            governors.append((metric, frozenset(narrowed_by)))
    return governors


def with_published_flags(config: PackageConfig) -> PackageConfig:
    """``config`` with each ``MeasureConfig.publish`` as the package's YAML loads it: a
    measure some metric publishes is published."""

    published = {published_measure(metric) for metric in config.metric_recipes}
    return replace(
        config,
        measures=[
            replace(row, publish=row.publish or row.id in published) for row in config.measures
        ],
    )


def building_block_measures(config: PackageConfig) -> frozenset[str]:
    """Unpublished measures (see ``MeasureConfig.publish``) that a metric governs."""

    unpublished = {measure.id for measure in config.measures if not measure.publish}
    return frozenset(
        measure_id
        for metric in config.metric_recipes
        for measure_id, narrowed in _measure_reads(expr_to_dict(metric.expression))
        if narrowed and measure_id in unpublished
    )


def unoffered_measures(config: PackageConfig) -> frozenset[str]:
    """Every unpublished measure; building blocks are among them."""

    return frozenset(measure.id for measure in config.measures if not measure.publish)
