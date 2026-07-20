"""Plan compact mode — trim blocked payloads for agent token budgets.

Per the audit, blocked entries used to dump multi-kilobyte
relationship-path analysis inline. Default verbosity is now ``compact``
and strips ``why_blocked.details`` plus the duplicated
``blocked_requirements`` / ``recovery_hints`` arrays.
"""

from __future__ import annotations

import json

from tests.plan_candidate_envelope import plan_candidate_envelope

_INTENT = "new vs returning customer revenue by month"


def test_plan_compact_blocked_is_substantially_smaller_than_full(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        compact = plan_candidate_envelope(runtime, intent=_INTENT, limit=3, verbosity="compact")
        full = plan_candidate_envelope(runtime, intent=_INTENT, limit=3, verbosity="full")

        compact_blocked = list(compact.get("blocked", []) or [])
        full_blocked = list(full.get("blocked", []) or [])

        compact_size = len(json.dumps(compact_blocked, default=str))
        full_size = len(json.dumps(full_blocked, default=str))

        # Both modes must surface the same count of blocked entries.
        assert len(compact_blocked) == len(full_blocked)
        # And the same first blocked code so downstream branching still works.
        if compact_blocked and full_blocked:
            assert compact_blocked[0].get("code") == (full_blocked[0].get("why_blocked") or {}).get(
                "code"
            )

        # Compact must be meaningfully smaller (the audit cited 3KB+ per entry).
        if full_blocked:
            assert compact_size < full_size * 0.5, (
                f"compact size {compact_size} must be < 50% of full size {full_size}"
            )

        # Compact entries must expose the keys an agent uses for next steps.
        if compact_blocked:
            entry = compact_blocked[0]
            for key in (
                "object_id",
                "code",
                "message",
                "details_url",
                "closest_legal_alternatives",
            ):
                assert key in entry, f"compact blocked entry missing {key}: {list(entry)}"

        # Echoed verbosity tells the caller what mode they got.
        assert compact.get("verbosity") == "compact"
        assert full.get("verbosity") == "full"
    finally:
        runtime.close()
