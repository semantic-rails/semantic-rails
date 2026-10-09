"""One refusal boundary for time requests on packages that declare no time."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import SemanticLayerError
from .expressions import _opaque_expression_data, expr_to_dict
from .schema import PackageConfig


def require_temporal_support(config: PackageConfig, *, requested: bool = True) -> None:
    if requested and not config.temporal_roles:
        raise SemanticLayerError(
            "INVALID_TEMPORAL_ROLE",
            "This package declares no time (no temporal roles). Ask a question without "
            "time, or declare a times: entry on a model before requesting time analysis.",
            details={"available_temporal_roles": []},
        )


def validate_temporal_support(config: PackageConfig, payload: Mapping[str, Any]) -> None:
    """Check time intent before normalization can discard empty or implicit time shapes.

    Follow authored metric/measure expressions, including nested predicates, but
    never interpret literal values, filter values or caller context as requests.
    """
    if config.temporal_roles:
        return
    seen: set[str] = set()
    recipes: dict[str, Any] = {row.id: row for row in config.metric_recipes}
    measures: dict[str, Any] = {row.id: row for row in config.measures}
    time_kinds = {
        "cumulative",
        "rolling",
        "prior_period",
        "period_to_date",
    }
    time_keys = {
        "temporal_role",
        "temporal_role_overrides",
        "grain",
        "time_grain",
        "time_alignment",
        "window",
        "window_unit",
        "anchor",
    }

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            kind = str(node.get("kind", "")).strip()
            if kind == "literal":
                return
            require_temporal_support(config, requested=kind in time_kinds)
            require_temporal_support(
                config,
                requested=str(node.get("aggregation", "")).strip() in {"first_value", "last_value"},
            )
            for key, child in node.items():
                if key == "policy_context" or _opaque_expression_data(node, key):
                    continue
                require_temporal_support(
                    config,
                    requested=(key == "time" and child is not None)
                    or (key in time_keys and bool(child)),
                )
                if key in {"metric", "metric_recipe", "basis_metric", "measure"} and isinstance(
                    child, str
                ):
                    reference = child.strip()
                    objects = measures if key == "measure" else recipes
                    obj = objects.get(reference)
                    if obj is None:
                        obj = next(
                            (
                                row
                                for row in objects.values()
                                if reference in {row.name, row.label, *row.aliases}
                            ),
                            None,
                        )
                    if obj is not None and obj.id not in seen:
                        seen.add(obj.id)
                        if key == "measure":
                            visit(expr_to_dict(obj.expr))
                        else:
                            require_temporal_support(
                                config, requested=bool(obj.temporal_role or obj.window_spec)
                            )
                            visit(obj.filter_spec)
                            visit(expr_to_dict(obj.expression))
                visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)

    visit(payload)


def _date_key(value: Any) -> str:
    return str(value or "").split("T", 1)[0].split(" ", 1)[0]


def _range_intersects(start: str, end: str, window_start: str, window_end: str) -> bool:
    lower_ok = not window_end or not start or start < window_end
    upper_ok = not window_start or not end or end > window_start
    return lower_ok and upper_ok
