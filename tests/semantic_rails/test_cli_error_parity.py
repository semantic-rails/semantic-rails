"""CLI error-path parity with the HTTP/MCP envelopes.

Two regressions covered here:

1. Enrichment gap — the HTTP boundary
   (``SemanticHTTPService.exception_payload``) and the MCP adapter
   (``_error_response``) run ``enrich_object_not_found`` so an
   ``OBJECT_NOT_FOUND`` envelope carries ``closest_matches`` for typo'd
   ids. The CLI exception handler never called the enrichers, so the same
   typo printed an envelope with no suggestions and a misleading
   "No close matches in the catalog" hint.

2. Triple-printed envelope — the CLI serialized the same structured issue
   three times per failure: under ``error``, again under ``errors[0]``,
   and a third partial copy via a top-level ``recovery_hints`` hoist. The
   issue must now print exactly once, under ``error``, with exit code 1
   preserved.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# A near-miss of the real jaffle_shop metric ids (metric.sales.*_revenue),
# guaranteed to produce closest_matches when enrichment runs.
TYPO_OBJECT_ID = "metric.jaffle_revenu"


def _run_cli_error(*args: str) -> tuple[dict, subprocess.CompletedProcess]:
    """Invoke the CLI via ``python -m semantic_rails`` and return the parsed
    JSON stdout plus the completed process (for exit-code assertions)."""
    proc = subprocess.run(
        [sys.executable, "-m", "semantic_rails", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if not proc.stdout.strip():
        raise AssertionError(f"CLI produced no stdout. args={args!r} stderr={proc.stderr!r}")
    return json.loads(proc.stdout), proc


def test_cli_object_not_found_is_enriched_with_closest_matches() -> None:
    payload, proc = _run_cli_error(
        "inspect", "--package", "jaffle_shop", "--object-id", TYPO_OBJECT_ID
    )
    assert proc.returncode == 1
    assert payload["ok"] is False
    assert payload["status"] == "error"
    error = payload["error"]
    assert error["code"] == "OBJECT_NOT_FOUND"
    matches = list(error["details"].get("closest_matches") or [])
    assert matches, (
        "CLI OBJECT_NOT_FOUND envelopes must carry closest_matches like the "
        f"HTTP and MCP surfaces; got details={error['details']!r}"
    )
    assert all(str(match).startswith("metric.") for match in matches), matches
    hints = list(error.get("recovery_hints") or [])
    assert hints, "structured recovery_hints must survive on the issue"
    assert any(hint.get("closest_matches") for hint in hints), hints


def test_cli_closest_matches_match_http_surface(runtime_factory) -> None:
    """The CLI and HTTP envelopes for the same typo must suggest the same
    near-miss ids — one enrichment chain, no per-surface drift."""
    from semantic_rails.errors import SemanticLayerError
    from semantic_rails.http_core import SemanticHTTPService

    runtime = runtime_factory("jaffle_shop")
    try:
        service = SemanticHTTPService(runtime)
        try:
            service.handle("POST", "/inspect", {"object_id": TYPO_OBJECT_ID})
            raise AssertionError("expected OBJECT_NOT_FOUND from /inspect")
        except SemanticLayerError as exc:
            http_payload, http_status = service.exception_payload(exc, stage="inspect")
    finally:
        runtime.close()
    assert http_status == 400
    http_matches = list(http_payload["error"]["details"]["closest_matches"])
    assert http_matches

    cli_payload, _ = _run_cli_error(
        "inspect", "--package", "jaffle_shop", "--object-id", TYPO_OBJECT_ID
    )
    cli_matches = list(cli_payload["error"]["details"]["closest_matches"])
    assert cli_matches == http_matches


def test_cli_prints_error_envelope_exactly_once() -> None:
    payload, proc = _run_cli_error(
        "inspect", "--package", "jaffle_shop", "--object-id", TYPO_OBJECT_ID
    )
    assert proc.returncode == 1
    # The structured issue appears once, under `error` — not repeated under
    # `errors[0]` nor partially re-hoisted as a top-level `recovery_hints`.
    assert proc.stdout.count('"code": "OBJECT_NOT_FOUND"') == 1, (
        "the OBJECT_NOT_FOUND issue must be serialized exactly once:\n" + proc.stdout
    )
    assert "errors" not in payload, "duplicate `errors` copy must be gone"
    assert "recovery_hints" not in payload, (
        "top-level recovery_hints hoist must be gone; hints live on error.recovery_hints"
    )
    assert payload["error"]["recovery_hints"], (
        "recovery_hints must remain reachable at error.recovery_hints"
    )
