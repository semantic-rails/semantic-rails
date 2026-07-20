"""Structured per-object metadata blocks consumed by inspect/discover output.

Each helper extracts a small, well-defined slice of attributes from a
catalog object (measure / metric recipe / dimension) into the JSON shape
the metadata payloads splat into their card responses. These are pure
structural extractors — no traversal, no policy, no relevance — so they
live together separately from expression introspection (``_expr_*``) and
from guidance (``_metric_guidance``, ``_aggregation_guidance``).
"""

from __future__ import annotations

from typing import Any

from ..operational import operational_payload


def _comparison_metadata(obj: Any) -> dict[str, Any]:
    return {
        "comparison_family": str(getattr(obj, "comparison_family", "") or ""),
        "comparison_mode": str(getattr(obj, "comparison_mode", "") or ""),
        "comparison_peers": list(getattr(obj, "comparison_peers", []) or []),
        "clock_variants": list(getattr(obj, "clock_variants", []) or []),
        "preferred_companion_metrics": list(getattr(obj, "preferred_companion_metrics", []) or []),
    }


def _operational_metadata(obj: Any) -> dict[str, Any]:
    return {"operational": operational_payload(obj)}


def _time_contract_metadata(obj: Any) -> dict[str, Any]:
    compatible = list(getattr(obj, "compatible_temporal_roles", []) or [])
    default_role = str(
        getattr(obj, "default_temporal_role", "")
        or getattr(obj, "temporal_role", "")
        or (compatible[0] if compatible else "")
    )
    return {
        "default_temporal_role": default_role,
        "compatible_temporal_roles": compatible,
    }


def _review_metadata(obj: Any) -> dict[str, Any]:
    meta = dict(getattr(obj, "meta", {}) or {})
    return {"meta": meta, "review_priority": str(meta.get("review_priority", "") or "")}


def _example_test_metadata(obj: Any) -> dict[str, Any]:
    return {
        "example_entries": list(getattr(obj, "example_entries", []) or []),
        "tests": list(getattr(obj, "tests", []) or []),
    }


def _public_object_type(kind: str) -> str:
    return {"metric": "metric"}.get(kind, kind)
