"""Discover answers the same call with the same bytes in every process.

Python seeds its string hashes per process, so anything a response takes from a set's order
(the reasons a row matched, and the order its score's amounts sum in) would change between two
runs of one call. A score that moves in its last digit could also break a tie between two rows
the other way. Each case runs discover in subprocesses with different hash seeds and compares
both channels byte for byte: the text an agent reads, and the structured content.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEEDS = ("0", "1", "2")

_SCRIPT = """
import json, sys
from semantic_rails.mcp import SemanticLayerMCPAdapter, json_text
from semantic_rails.runtime import Runtime

package, terms = sys.argv[1], json.loads(sys.argv[2])
runtime = Runtime.from_path(package)
adapter = SemanticLayerMCPAdapter(runtime)
try:
    for term in terms:
        for verbosity in ("minimal", "compact", "full"):
            body = adapter.call_tool("discover", {"terms": term, "verbosity": verbosity})
            # A call's own id and timing differ by design.
            body.pop("request_id", None)
            body.pop("timing_ms", None)
            print(json.dumps({"ok": body.get("ok"), "text": json_text(body), "structured": json.dumps(body)}))
finally:
    runtime.close()
"""

CASES = {
    "configs/semantic_rails/jaffle_shop": (
        "customers",
        "How many customers ordered by store?",
        "How many customers bought each product type?",
        "Top stores by revenue",
        "",
    ),
    "tests/integration/correctness/shop": ("customers", "orders by channel", ""),
}


def _discover(package: str, terms: tuple[str, ...], seed: str) -> list[dict[str, str]]:
    completed = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(ROOT / package), json.dumps(terms)],
        capture_output=True,
        text=True,
        timeout=240,
        env={**os.environ, "PYTHONHASHSEED": seed},
        cwd=ROOT,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]


@pytest.mark.parametrize("package", sorted(CASES))
def test_discover_is_byte_identical_across_hash_seeds(package: str) -> None:
    terms = CASES[package]
    first, *others = (_discover(package, terms, seed) for seed in SEEDS)
    assert len(first) == 3 * len(terms)
    # Every call answers, so the comparison covers the ranked rows and not a refusal.
    assert all(response["ok"] is True for response in first)
    for seed, responses in zip(SEEDS[1:], others, strict=True):
        for index, (expected, seen) in enumerate(zip(first, responses, strict=True)):
            call = f"{terms[index // 3]!r} at seed {seed}"
            assert seen["text"] == expected["text"], call
            assert seen["structured"] == expected["structured"], call
