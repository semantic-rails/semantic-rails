"""Cache-hit latency gate of ``scripts/benchmark_plan.py``."""

from __future__ import annotations

import pytest

from scripts.benchmark_plan import _compile_after_plan, _gate_exit_code, _load_cases, _run_case
from semantic_rails.planner import plan_payload

PAYLOAD = {"status": "ok", "best": {"query": {"select": [{"metric": "m"}]}}}


@pytest.fixture()
def runtime(runtime_factory):
    return runtime_factory("jaffle_shop")


class FakeRuntime:
    """Compile stub that advances a fake clock by one scripted duration per call."""

    def __init__(self, durations_ms: list[float], hits: list[bool]) -> None:
        self.now = 0.0
        self.durations_ms = list(durations_ms)
        self.hits = list(hits)
        self.calls = 0

    def clock(self) -> float:
        return self.now

    def compile(self, query: dict) -> dict:
        assert query["verbosity"] == "full"
        self.now += self.durations_ms[self.calls] / 1000
        hit = self.hits[self.calls]
        self.calls += 1
        return {"ok": True, "compile_stats": {"cache_hit": hit, "cache_lookup_ms": 0.1}}


def _gate(durations_ms: list[float], hits: list[bool]) -> tuple[dict, list[dict]]:
    runtime = FakeRuntime(durations_ms, hits)
    probe = _compile_after_plan(
        runtime, PAYLOAD, repeats=len(durations_ms) - 1, clock=runtime.clock
    )
    fast = {"name": "fast", "compile_cache_hit": True, "compile_ms": 3.0}
    probed = {
        "name": "probed",
        "compile_cache_hit": probe["cache_hit"],
        "compile_ms": probe["elapsed_ms"],
    }
    rows = [
        {
            "expected": "answer",
            "actionable": True,
            "status": "ok",
            "full_status": "ok",
            "best_bytes": 10,
            "full_bytes": 20,
            "compile_attempted": True,
            **row,
        }
        for row in (fast, fast, fast, fast, fast, probed)
    ]
    return probe, rows


@pytest.mark.parametrize(
    ("durations_ms", "hits", "exit_code", "message"),
    [
        pytest.param([30, 3, 3, 3, 3], [True] * 5, 0, "### Gate: PASS", id="one-slow-sample"),
        pytest.param(
            [30, 26, 27, 28, 29],
            [True] * 5,
            1,
            "- latency regression: cache-hit compile p95=26.000ms > 25.000ms",
            id="every-hit-slow",
        ),
        pytest.param(
            [3, 3, 3, 3, 3],
            [False] * 5,
            1,
            "- cache regression: plan-warmed compile misses: ['probed']",
            id="first-compile-misses",
        ),
        pytest.param(
            [3, 3, 3, 3, 3],
            [True, True, False, True, True],
            1,
            "- cache regression: plan-warmed compile misses: ['probed']",
            id="repeat-misses",
        ),
    ],
)
def test_cache_hit_gate(durations_ms, hits, exit_code, message, capsys) -> None:
    probe, rows = _gate(durations_ms, hits)

    assert _gate_exit_code(rows, max_best_bytes=6_500, max_cache_hit_p95_ms=25.0) == exit_code
    assert message in capsys.readouterr().out.splitlines()
    assert probe["first_elapsed_ms"] == pytest.approx(durations_ms[0])


def test_cache_miss_stops_repeating() -> None:
    runtime = FakeRuntime([40, 3, 3], [False, True, True])

    probe = _compile_after_plan(runtime, PAYLOAD, repeats=2, clock=runtime.clock)

    assert runtime.calls == 1
    assert probe["cache_hit"] is False
    assert probe["elapsed_ms"] == pytest.approx(40)


def test_public_plan_warms_compile_cache(runtime) -> None:
    payload = plan_payload(runtime, intent="total orders")
    assert payload["status"] == "ok"

    probe = _compile_after_plan(runtime, payload, repeats=1)

    assert probe["ok"], probe
    assert probe["cache_hit"], probe


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [
        ("ordered_vs_delivered_revenue", "unrealizable", "PLAN_UNCONNECTED"),
        ("ambiguous_customer_status", "unrealizable", "PLAN_UNCONNECTED"),
        ("historical_revenue_by_segment", "low_confidence", "PLAN_INTENT_COVERAGE_GAP"),
        (
            "signup_to_send_adoption_for_high_order_rate_stores",
            "low_confidence",
            "PLAN_UNASKED_GROUPING",
        ),
    ],
)
def test_held_case_contract(runtime, name, status, code) -> None:
    case = next(case for case in _load_cases() if case["name"] == name)
    payload = plan_payload(runtime, intent=case["intent"])
    assert payload["status"] == status
    assert payload["why"]["code"] == code
    assert "execute" not in payload.get("next", {}).get("ready_for", [])
    assert case["expected"] == "clarify"
    assert case["expected_status"] == status
    assert case["forbid_ready_for_execute"]
    row = _run_case(runtime, case)
    assert row["missing_expected_ids"] == []
    assert row["missing_expected_query_patch"] == []
    assert not row["unexpected_execute_ready"]


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [("unrealizable", 0), ("out_of_scope", 0), ("ok", 1), ("low_confidence", 1)],
)
def test_size_gate_requires_a_smaller_draft_only_when_one_can_be_offered(
    status, expected_exit
) -> None:
    _, rows = _gate([3], [True])
    rows[0].update(status=status, full_status=status, best_bytes=20, full_bytes=20)
    assert _gate_exit_code(rows, max_best_bytes=6_500, max_cache_hit_p95_ms=25) == expected_exit
