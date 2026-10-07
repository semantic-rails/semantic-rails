"""Which metrics govern a measure, read from the metric expressions alone.

A metric governs a measure when it aggregates it through a filter: an aggregate with a
``filter``, or a scoped aggregate with ``where`` or ``predicates`` ("Active stores", the retail
stores of an all-kinds store count). A metric publishes a measure when its whole expression
is that measure's aggregate with no filter, as a measure's own ``publish`` and a
``kind: aggregate`` metric make.

A measure authored with ``publish: false`` that a metric governs and no metric publishes is a
building block: ``discover`` doesn't offer it and ``plan`` answers with the metrics that
govern it. It stays queryable by id.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from typing import Any

from ..expressions import collect_object_references, expr_to_dict
from ..schema import MetricConfig, PackageConfig

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

    node = expr_to_dict(metric.expression)
    if node.get("kind") not in _AGGREGATES or not node.get("measure"):
        return None
    if node.get("window") or node.get("anchor"):
        return None
    narrowing = {key: node[key] for key in _NARROWING if node.get(key)}
    return str(node["measure"]), str(node.get("aggregation") or ""), narrowing


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


def population_governors(
    config: PackageConfig, measure_id: str
) -> list[tuple[MetricConfig, frozenset[str]]]:
    """The metrics that narrow ``measure_id``'s rows, each with the dimensions it narrows on.

    A metric narrows a measure when an aggregate in it, over that measure or another one, has
    a narrowing that reads a dimension of the measure's entity: "New teams" counts the creation
    events of customer teams, so it narrows a count of every team by the team's class.
    """

    entity = next((row.entity for row in config.measures if row.id == measure_id), None)
    own = {row.id for row in config.dimensions if row.entity == entity}
    governors = []
    for metric in config.metric_recipes:
        narrowed_by = frozenset(
            reference
            for _, narrowing in _measure_reads(expr_to_dict(metric.expression))
            for reference in collect_object_references(narrowing, config)
            if reference in own
        )
        if narrowed_by:
            governors.append((metric, narrowed_by))
    return governors


def with_published_flags(config: PackageConfig) -> PackageConfig:
    """``config`` with each ``MeasureConfig.publish`` as the package's YAML loads it.

    A measure some metric publishes is published. Without ``schema_strict`` every other
    measure was authored ``publish: false``, since the loader publishes the rest itself; a
    package written back, with each metric spelled out, then reads the same.
    """

    published = {published_measure(metric) for metric in config.metric_recipes}
    strict = config.package.schema_strict
    return replace(
        config,
        measures=[
            replace(row, publish=row.id in published or (strict and row.publish))
            for row in config.measures
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
