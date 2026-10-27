"""An output column named after a governed metric or measure is that metric or measure.

A select may compute anything inline and name the result, but a name the package governs (a
metric's or measure's id, its name, or the column it answers under) would present a different
number as the governed one: an order count as `revenue_usd`, or the maximum of the revenue
measure as `revenue_usd`. Such a select is refused with the governed object it impersonates and
a name to use instead. Selecting the governed object itself under its own name is unchanged,
whether by reference, as the recipe the planner inlines, as a measure's own default aggregate,
or rounded (`ROUND` changes precision, not what is counted).

The names are the caller's: planning reads the package as the caller sees it, so a name only a
hidden metric or measure holds is no governed name for that caller, and the refusal never
reveals that the hidden object exists.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from typing import Any

from ..ast import NormalizedQuery
from ..errors import SemanticLayerError
from ..expressions import AggregateExpr, CallExpr, MeasureRefExpr, MetricRecipeRefExpr
from ..schema import MeasureConfig, MetricConfig, PackageConfig

Owner = tuple[str, str]  # ("metric" | "measure", object id)


def _names(object_id: str, name: str) -> set[str]:
    """The names a governed object answers to: its id, its name and its last segment."""
    return {value for value in (object_id, name, object_id.rsplit(".", 1)[-1]) if value}


def _governed_owners(config: PackageConfig) -> dict[str, set[Owner]]:
    owners: dict[str, set[Owner]] = {}
    for metric in config.metric_recipes:
        for name in _names(metric.id, metric.name):
            owners.setdefault(name, set()).add(("metric", metric.id))
    for measure in config.measures:
        for name in _names(measure.id, measure.name):
            owners.setdefault(name, set()).add(("measure", measure.id))
    return owners


def _plain_measure(expr: Any, measures: dict[str, MeasureConfig]) -> str:
    """The measure ``expr`` reads as its own default aggregate, or "" for anything else."""
    if isinstance(expr, MeasureRefExpr):
        extra: Any = expr.parameters
    elif isinstance(expr, AggregateExpr):
        extra = expr.parameters or expr.filter or expr.window
    else:
        return ""
    measure = measures.get(expr.measure)
    if measure is None or extra:
        return ""
    stock_sum = measure.measure_class == "semi_additive" and expr.aggregation == "sum"
    if expr.aggregation not in ("", measure.default_aggregation) and not stock_sum:
        return ""
    return measure.id


def _canonical(expr: Any, measures: dict[str, MeasureConfig]) -> Any:
    """``expr`` in one spelling: a plain aggregate reads as its measure's reference, with the
    measure's default aggregation filled in (the form the planner inlines a recipe in)."""
    if isinstance(expr, AggregateExpr) and not (expr.filter or expr.window):
        expr = MeasureRefExpr(expr.measure, expr.aggregation, expr.temporal_role, expr.parameters)
    if isinstance(expr, MeasureRefExpr) and expr.measure in measures:
        measure = measures[expr.measure]
        # A stock read sums its members at the read day, so `sum` over a semi-additive measure is
        # its default read (the planner selects a balance metric this way).
        stock_sum = measure.measure_class == "semi_additive" and expr.aggregation == "sum"
        if not expr.aggregation or stock_sum:
            return replace(expr, aggregation=measure.default_aggregation)
        return expr
    if is_dataclass(expr) and not isinstance(expr, type):
        changes = {
            item.name: _canonical(getattr(expr, item.name), measures) for item in fields(expr)
        }
        return replace(expr, **changes)
    if isinstance(expr, list):
        return [_canonical(item, measures) for item in expr]
    if isinstance(expr, tuple):
        return tuple(_canonical(item, measures) for item in expr)
    return expr


def _computes(expr: Any, owners: set[Owner], config: PackageConfig) -> bool:
    """Whether ``expr`` is one of ``owners``: the metric itself (by reference, or as the recipe
    the planner inlines), or a measure's own default aggregate, directly or as a metric that is
    nothing more than that aggregate."""
    measures = {measure.id: measure for measure in config.measures}
    metrics = {metric.id: metric for metric in config.metric_recipes}

    def plain_measure_of(metric: MetricConfig | None) -> str:
        if metric is None or metric.filter_spec or metric.window_spec:
            return ""
        return _plain_measure(metric.expression, measures)

    if (
        isinstance(expr, CallExpr)
        and expr.name.upper() == "ROUND"
        and expr.args
        and not expr.distinct
    ):
        # Rounding changes a governed number's precision, not what it counts.
        return _computes(expr.args[0], owners, config)
    owned_metrics = [metrics[object_id] for kind, object_id in owners if kind == "metric"]
    if isinstance(expr, MetricRecipeRefExpr):
        if ("metric", expr.metric_recipe) in owners:
            return True
        measure_id = plain_measure_of(metrics.get(expr.metric_recipe))
    else:
        canonical = _canonical(expr, measures)
        if any(_canonical(metric.expression, measures) == canonical for metric in owned_metrics):
            return True
        measure_id = _plain_measure(expr, measures)
    if not measure_id:
        return False
    return ("measure", measure_id) in owners or any(
        plain_measure_of(metric) == measure_id for metric in owned_metrics
    )


def _label(owner: Owner, config: PackageConfig) -> str:
    kind, object_id = owner
    objects: list[MetricConfig] | list[MeasureConfig] = (
        config.metric_recipes if kind == "metric" else config.measures
    )
    found = next((item for item in objects if item.id == object_id), None)
    label = getattr(found, "label", "") if found is not None else ""
    return (
        f"the governed {kind} '{label}' ({object_id})"
        if label
        else f"the governed {kind} {object_id}"
    )


def _suggestion(
    expr: Any, alias: str, owners_by_name: dict[str, set[Owner]], config: PackageConfig
) -> str:
    """A name the select may use: its own object's name where it has one, else a new name."""
    candidates: list[str] = []
    if isinstance(expr, MetricRecipeRefExpr):
        candidates.append(expr.metric_recipe.rsplit(".", 1)[-1])
    if isinstance(expr, (MeasureRefExpr, AggregateExpr)):
        measure = expr.measure.rsplit(".", 1)[-1]
        candidates.append(f"{expr.aggregation}_{measure}" if expr.aggregation else measure)
    candidates.append(f"{alias.rsplit('.', 1)[-1]}_calculated")
    for candidate in candidates:
        owners = owners_by_name.get(candidate)
        if not owners or _computes(expr, owners, config):
            return candidate
    return candidates[-1]


def refuse_impersonating_aliases(query: NormalizedQuery, config: PackageConfig) -> None:
    owners_by_name = _governed_owners(config)
    for index, item in enumerate(query.select):
        owners = owners_by_name.get(item.as_)
        if not owners or item.expression is None or _computes(item.expression, owners, config):
            continue
        owner = min(owners, key=lambda found: (found[0] != "metric", found[1]))
        suggestion = _suggestion(item.expression, item.as_, owners_by_name, config)
        raise SemanticLayerError(
            "OUTPUT_NAME_GOVERNED",
            (
                f"'{item.as_}' is the name of {_label(owner, config)}, but select[{index}] "
                "computes something else, so its numbers would read as the governed one's. "
                f"Name the column something of its own, such as '{suggestion}', or select "
                f"{owner[1]} itself."
            ),
            details={
                "path": f"select[{index}].as",
                "alias": item.as_,
                "governed_object": owner[1],
                "governed_kind": owner[0],
                "suggested_alias": suggestion,
            },
        )
