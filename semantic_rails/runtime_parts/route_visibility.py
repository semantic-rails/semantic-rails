"""Project serialized route plans without changing the compiled execution plan."""

from typing import Any

from ..fanout import visible_route
from ..schema import PackageConfig


def project_route_response(
    response: dict[str, Any], config: PackageConfig, hidden_ids: frozenset[str] | None
) -> None:
    if hidden_ids == frozenset() or (
        hidden_ids is None
        and not any(policy.kind == "object_visibility" for policy in config.semantic_policies)
    ):
        return

    def visible(path: list[str], target: str = "") -> bool:
        return visible_route(config, target, path, hidden_ids)

    def project(value: Any) -> Any:
        """Project a known route structure, without descending into query or expression data."""
        if isinstance(value, list):
            return [row for item in value if (row := project(item)) is not None]
        if not isinstance(value, dict):
            return value
        if "relationship_id" in value and not visible([value["relationship_id"]]):
            return None
        for key in ("path", "chosen_path", "relationship_path", "source_path"):
            if key in value and not visible(value[key]):
                return None
        if (
            isinstance(value.get("relationships"), list)
            and all(isinstance(item, str) for item in value["relationships"])
            and not visible(value["relationships"])
        ):
            return None
        out = dict(value)
        if "selected_paths" in value:
            paths = value["selected_paths"]
            out["selected_paths"] = (
                {target: path for target, path in paths.items() if visible(path, target)}
                if isinstance(paths, dict)
                else [
                    row for row in paths if visible(row["relationship_ids"], row["target_entity"])
                ]
            )
        if "candidate_paths" in value:
            paths = value["candidate_paths"]
            out["candidate_paths"] = (
                {
                    target: [path for path in candidates if visible(path, target)]
                    for target, candidates in paths.items()
                    if visible([], target)
                }
                if isinstance(paths, dict)
                else [path for path in paths if visible(path)]
            )
        if "chosen_paths" in value:
            out["chosen_paths"] = {
                target: {**row, "candidates": [path for path in row["candidates"] if visible(path)]}
                for target, row in value["chosen_paths"].items()
                if visible(row["selected"], target)
            }
        # These are route containers in PathSelection, RewriteStep, and join-plan rows.
        for key in ("analysis", "details", "contracts", "paths"):
            if key not in value:
                continue
            if key == "paths" and isinstance(value[key], dict):
                if any(not visible(path, target) for target, path in value[key].items()):
                    return None
                continue
            projected = project(value[key])
            if projected is None:
                return None
            out[key] = projected
        return out

    def at(value: Any, location: tuple[str, ...]) -> Any:
        if not location:
            return project(value)
        key, *rest = location
        if key == "*":
            if isinstance(value, list):
                return [row for item in value if (row := at(item, tuple(rest))) is not None]
            return {
                name: row
                for name, item in value.items()
                if (row := at(item, tuple(rest))) is not None
            }
        if isinstance(value, dict) and key in value:
            projected = at(value[key], tuple(rest))
            return {**value, key: {} if projected is None else projected}
        return value

    # Locations come from LogicalPlan, ExplainArtifact, and their response summaries.
    # Never walk normalized_query, query, resolved_ids, expression values, or SQL ASTs.
    locations = [("explain",), ("provenance_summary",), ("hop_profile", "targets", "*")]
    prefix: tuple[str, ...]
    for prefix in (("logical_plan",), ("explain", "logical_plan")):
        locations.extend(
            [
                prefix,
                (*prefix, "measure_plans", "*", "path_selections", "*"),
                (*prefix, "rewrite_steps", "*"),
            ]
        )
    for prefix in (("logical_plan",), ("explain", "logical_plan"), ("explain",)):
        locations.extend(
            [
                (*prefix, "fanout_strategy", "root_paths", "*"),
                (*prefix, "fanout_strategy", "leaf_paths", "*", "*"),
            ]
        )
    for prefix in ((), ("explain",)):
        locations.extend(
            [
                (*prefix, "physical_plan", "nodes", "*", "details"),
                (*prefix, "performance_plan", "joins_before_aggregate", "*"),
                (*prefix, "performance_plan", "joins_after_aggregate", "*"),
            ]
        )
    locations.extend(
        [("explain", "rewrite_strategy", "steps", "*"), ("trace", "selected", "paths", "*")]
    )
    for location in locations:
        response.update(at(response, location))
