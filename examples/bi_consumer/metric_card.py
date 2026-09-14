"""Small BI app binding: persist an identity, execute its governed query by reference.

The caller selects an authenticated runtime/package. This example neither
interprets semantic expressions nor constructs SQL or authorization claims.
"""

from collections.abc import Callable, Mapping
from typing import Any


def render_metric_card(
    artifact: Mapping[str, Any],
    identity: tuple[str, str],
    execute: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    if (
        type(artifact.get("contract_format_version")) is not int
        or artifact["contract_format_version"] != 1
    ):
        raise ValueError("Unsupported metric portability contract major")
    namespace, metric_id = identity
    if artifact["package"]["namespace"] != namespace:
        raise ValueError("Metric namespace does not match selected package")
    matches = [row for row in artifact["metrics"] if row["id"] == metric_id]
    if len(matches) != 1:
        raise ValueError("Metric identity is missing or ambiguous")
    metric = matches[0]
    result = execute(
        {
            "version": 1,
            "select": [{"expression": {"kind": "metric", "metric": metric_id}, "as": "value"}],
        }
    )
    return {"identity": identity, "label": metric["label"], "result": result}
