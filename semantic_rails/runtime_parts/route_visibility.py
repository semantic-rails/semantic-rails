"""Project serialized route plans without changing the compiled execution plan."""

from typing import Any

from ..compiler_parts.indexes import get_package_analysis
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
    root = response.get("logical_plan", {}).get("root_entity", "")
    relationships = get_package_analysis(config).relationships

    def project(value: Any, start: str) -> Any:
        if isinstance(value, list):
            return [row for item in value if (row := project(item, start)) != {}]
        if not isinstance(value, dict):
            return value
        start = str(value.get("source_entity", value.get("root_entity", start)) or "")
        if "relationship_id" in value:
            relationship = relationships[value["relationship_id"]]
            if not visible_route(config, relationship.source_entity, [relationship.id], hidden_ids):
                return {}
        for key in ("path", "chosen_path", "relationship_path"):
            if key in value and not visible_route(config, start, value[key], hidden_ids):
                return {}
        if (
            isinstance(value.get("relationships"), list)
            and all(isinstance(item, str) for item in value["relationships"])
            and not visible_route(config, start, value["relationships"], hidden_ids)
        ):
            return {}
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key == "selected_paths":
                if isinstance(item, dict):
                    out[key] = {
                        target: path
                        for target, path in item.items()
                        if visible_route(config, start, path, hidden_ids)
                    }
                else:
                    out[key] = [
                        row
                        for row in item
                        if visible_route(config, start, row["relationship_ids"], hidden_ids)
                    ]
            elif key == "candidate_paths":
                if isinstance(item, dict):
                    out[key] = {
                        target: [
                            path for path in paths if visible_route(config, start, path, hidden_ids)
                        ]
                        for target, paths in item.items()
                        if visible_route(config, target, [], hidden_ids)
                    }
                else:
                    out[key] = [
                        path for path in item if visible_route(config, start, path, hidden_ids)
                    ]
            elif key == "chosen_paths":
                out[key] = {
                    target: {
                        **row,
                        "candidates": [
                            path
                            for path in row["candidates"]
                            if visible_route(config, start, path, hidden_ids)
                        ],
                    }
                    for target, row in item.items()
                    if visible_route(config, start, row["selected"], hidden_ids)
                }
            else:
                out[key] = project(item, start)
        return out

    for key in (
        "logical_plan",
        "explain",
        "hop_profile",
        "provenance_summary",
        "physical_plan",
        "performance_plan",
        "trace",
    ):
        if key in response:
            response[key] = project(response[key], root)
