"""Test-only mapper from ``plan`` to the old candidate envelope.

Production code only exposes the public ``plan`` payload. A handful of
older regression tests still assert candidate-envelope details; this
module keeps those tests pointed at ``plan_payload`` while the assertions
are migrated to the public response shape.
"""

from __future__ import annotations

from typing import Any

from semantic_rails.planner import plan_payload

_COMPACT_VALIDATION_DROP_KEYS: frozenset[str] = frozenset(
    {
        "explain",
        "logical_plan",
        "normalized_query",
        "output_columns",
        "provenance_summary",
        "policy_effects",
        "warehouse_capabilities",
        "methodology_hints",
        "request_context",
    }
)


def plan_candidate_envelope(
    runtime: Any,
    *,
    intent: str,
    partial_query: dict[str, Any] | None = None,
    limit: int = 5,
    verbosity: str = "compact",
) -> dict[str, Any]:
    payload = plan_payload(
        runtime,
        intent=intent,
        partial_query=partial_query,
        detail="full",
        limit=limit,
    )
    out: dict[str, Any] = {
        "candidate_envelope_version": 1,
        "intent": intent,
        "interpreted_intent": "",
        "unresolved_terms": [],
        "query_state": {},
        "candidates": [],
        "blocked": [],
        "comparison_bundle": [],
        "verbosity": verbosity,
    }
    why = payload.get("why")
    if payload.get("status") == "out_of_scope":
        if isinstance(why, dict) and why.get("code") == "LOW_RELEVANCE":
            out["low_relevance"] = why
        elif isinstance(why, dict):
            out["out_of_scope"] = why
        return out

    rows = [payload.get("best"), *list(payload.get("alternatives") or [])]
    rows = [row for row in rows if isinstance(row, dict)]
    if rows:
        out["interpreted_intent"] = rows[0].get("interpreted_intent") or ""
    comparison_bundle = _comparison_bundle(rows)
    out["comparison_bundle"] = comparison_bundle

    seen_primary_ids: set[str] = set()
    emitted = 0
    for row in rows:
        if emitted >= max(1, int(limit or 1)):
            break
        primary_id = _primary_resolved_id(row)
        if primary_id and primary_id in seen_primary_ids:
            continue
        if primary_id:
            seen_primary_ids.add(primary_id)
        candidate = _candidate_from_row(
            runtime, row, index=emitted, verbosity=verbosity, intent=intent
        )
        emitted += 1
        if payload.get("status") == "low_confidence":
            if payload.get("blocked"):
                continue
            out["blocked"].append(
                _blocked_from_candidate(
                    candidate,
                    why=why,
                    comparison_bundle=comparison_bundle,
                    verbosity=verbosity,
                )
            )
            continue
        out["candidates"].append(candidate)

    for row in list(payload.get("blocked") or []):
        out["blocked"].append(
            _blocked_from_plan_row(row, comparison_bundle=comparison_bundle, verbosity=verbosity)
        )

    if not out["candidates"] and out["blocked"]:
        codes = [
            str((row.get("why_blocked") or {}).get("code") or row.get("code") or "")
            for row in out["blocked"]
            if isinstance(row, dict)
        ]
        out["no_viable_candidates"] = {
            "code": "NO_VIABLE_CANDIDATES",
            "intent": intent,
            "blocked_codes": [code for code in codes if code],
            "recovery_hint": "Review blocked entries and choose a legal alternative.",
        }
    return out


def _primary_resolved_id(row: dict[str, Any]) -> str:
    for item in list(row.get("resolved") or []):
        if isinstance(item, dict) and item.get("id"):
            return str(item["id"])
    return ""


def _candidate_from_row(
    runtime: Any,
    row: dict[str, Any],
    *,
    index: int,
    verbosity: str,
    intent: str,
) -> dict[str, Any]:
    query = dict(row.get("query_ir") or {})
    validation = runtime.validate(query) if query else {"ok": False, "errors": []}
    candidate: dict[str, Any] = {
        "candidate_ir": query,
        "resolved": list(row.get("resolved") or []),
        "resolved_objects": [
            str(item.get("id", ""))
            for item in list(row.get("resolved") or [])
            if isinstance(item, dict) and item.get("id")
        ],
        "assumptions": _assumptions_for_row(row, intent),
        "confidence": round(max(0.25, 0.95 - (0.1 * index)), 3),
        "rationale": list(row.get("rationale") or []),
        "validation": _validation_for_verbosity(validation, verbosity),
        "performance_risk": dict(validation.get("performance_plan", {}) or {}).get(
            "risk_level", ""
        ),
        "compact_explanation": "Validated runtime query IR composed from governed semantic primitives.",
    }
    if str(verbosity or "").lower() == "minimal":
        return {
            "candidate_ir": candidate["candidate_ir"],
            "confidence": candidate["confidence"],
            "rationale": candidate["rationale"],
            "resolved_objects": candidate["resolved_objects"],
            "performance_risk": candidate["performance_risk"],
            "compact_explanation": candidate["compact_explanation"],
            "validation_ok": bool(validation.get("ok")),
        }
    return candidate


def _validation_for_verbosity(validation: dict[str, Any], verbosity: str) -> dict[str, Any]:
    if str(verbosity or "").lower() == "full":
        return validation
    return {
        key: value for key, value in validation.items() if key not in _COMPACT_VALIDATION_DROP_KEYS
    }


def _blocked_from_candidate(
    candidate: dict[str, Any],
    *,
    why: Any,
    comparison_bundle: list[dict[str, Any]],
    verbosity: str,
) -> dict[str, Any]:
    errors = list((why or {}).get("errors") or []) if isinstance(why, dict) else []
    first = dict(errors[0] if errors else why if isinstance(why, dict) else {})
    full = {
        **candidate,
        "why_blocked": first,
        "closest_legal_alternatives": comparison_bundle,
        "blocked_requirements": [first] if first else [],
        "recovery_hints": list(first.get("recovery_hints") or []),
        "comparison_bundle": comparison_bundle,
        "comparison_mode": (
            "coordinated_queries"
            if comparison_bundle
            or (candidate.get("resolved_objects") and len(candidate["resolved_objects"]) > 1)
            else ""
        ),
    }
    return _compact_blocked(full) if str(verbosity or "").lower() != "full" else full


def _blocked_from_plan_row(
    row: dict[str, Any], *, comparison_bundle: list[dict[str, Any]], verbosity: str
) -> dict[str, Any]:
    why = dict(row.get("why") or {})
    full = {
        "candidate_ir": dict(row.get("query_ir") or {}),
        "resolved": list(row.get("resolved") or []),
        "resolved_objects": [
            str(item.get("id", ""))
            for item in list(row.get("resolved") or [])
            if isinstance(item, dict) and item.get("id")
        ],
        "assumptions": [],
        "confidence": 0.4,
        "rationale": [],
        "why_blocked": why,
        "closest_legal_alternatives": comparison_bundle,
        "blocked_requirements": [why] if why else [],
        "recovery_hints": list(row.get("recovery_hints") or []),
        "comparison_bundle": comparison_bundle,
    }
    return _compact_blocked(full) if str(verbosity or "").lower() != "full" else full


def _compact_blocked(row: dict[str, Any]) -> dict[str, Any]:
    why = dict(row.get("why_blocked") or {})
    resolved_rows = list(row.get("resolved") or [])
    resolved_ids = list(row.get("resolved_objects") or [])
    object_id = resolved_ids[0] if resolved_ids else ""
    compact: dict[str, Any] = {
        "object_id": object_id,
        "resolved": resolved_rows,
        "resolved_objects": resolved_ids,
        "why_blocked": {
            "code": str(why.get("code", "") or ""),
            "message": str(why.get("message", "") or ""),
        },
        "code": str(why.get("code", "") or ""),
        "message": str(why.get("message", "") or ""),
        "details_url": f"plan?id={object_id}&detail=full" if object_id else "",
        "closest_legal_alternatives": list(row.get("closest_legal_alternatives") or []),
    }
    recovery_hints = list(row.get("recovery_hints") or []) or list(why.get("recovery_hints") or [])
    if recovery_hints:
        compact["recovery_hints"] = recovery_hints
        compact["why_blocked"]["recovery_hints"] = recovery_hints
    if row.get("comparison_bundle"):
        compact["comparison_bundle"] = row["comparison_bundle"]
    if row.get("comparison_mode"):
        compact["comparison_mode"] = row["comparison_mode"]
    return compact


def _comparison_bundle(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[1:3]:
        query = dict(row.get("query_ir") or {})
        resolved = list(row.get("resolved") or [])
        if not query or not resolved:
            continue
        out.append(
            {
                "query_patch": query,
                "resolved": resolved,
                "assumptions": [],
                "rationale": list(row.get("rationale") or []),
            }
        )
    return out


def _assumptions_for_row(row: dict[str, Any], intent: str) -> list[str]:
    query = dict(row.get("query_ir") or {})
    grouped = " ".join(str(item).lower() for item in list(query.get("group_by") or []))
    lowered = str(intent or "").lower()
    assumptions: list[str] = []
    if "status" in lowered and not any(
        token in grouped for token in ("status", "state", "membership", "segment", "type")
    ):
        assumptions.append("No reachable customer status dimension matched the request.")
    return assumptions
