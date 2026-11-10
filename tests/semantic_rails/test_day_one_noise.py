"""Day one with a package: warnings name what changes answers, in paths relative to the
package, and every write answers first with three lines."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from semantic_rails.architect_mcp import _first_warnings, create_architect_mcp_server
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.package_tools import promote_package_report


def _call(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(server.call_tool(name, arguments))[1]


def _created(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    server = create_architect_mcp_server(workspace_root=tmp_path)
    created = _call(
        server,
        "create_project",
        {"package_id": "growth", "expected_revision": "absent", "idempotency_key": "create"},
    )
    return server, created


def _add_ungoverned_measure(server: Any, created: dict[str, Any]) -> dict[str, Any]:
    return _call(
        server,
        "upsert_model",
        {
            "project_path": created["project_path"],
            "model_id": "events",
            "entity_key": "event",
            "relation": "raw_events",
            "primary_key": ["event_id"],
            "measures": {
                "net": {
                    "label": "Net",
                    "kind": "aggregate",
                    "expr": "amount * 2",
                    "value_type": "number",
                }
            },
            "expected_revision": created["revision"],
            "idempotency_key": "net",
        },
    )


def test_review_metadata_is_reported_by_the_promotion_check_only(tmp_path: Path) -> None:
    server, created = _created(tmp_path)
    assert _add_ungoverned_measure(server, created)["ok"] is True
    ref = PackageReference(source_path=created["project_path"])

    parse, _ = parse_config_report(ref)
    assert parse["ok"] is True
    assert not [row for row in parse["warnings"] if "meta." in row["message"]]

    promotion = promote_package_report(ref, environment="development")
    review = [row for row in promotion["advisories"] if row["code"] == "REVIEW_METADATA_MISSING"]
    assert review == [
        {
            "code": "REVIEW_METADATA_MISSING",
            "message": "public measure measure.growth.net should declare meta.owner_team, "
            "meta.review_priority, meta.change_risk",
            "details": {
                "object_id": "measure.growth.net",
                "missing": ["meta.owner_team", "meta.review_priority", "meta.change_risk"],
            },
        }
    ]
    # Advisory, never a blocker.
    assert promotion["blockers"] == []


def test_messages_name_paths_relative_to_the_package(tmp_path: Path) -> None:
    server, created = _created(tmp_path)
    package = Path(created["project_path"])
    (package / "models" / "core" / "broken.yml").write_text("model: [", encoding="utf-8")

    report, _ = parse_config_report(PackageReference(source_path=str(package)))

    assert report["ok"] is False
    error = report["errors"][0]
    assert error["code"] == "INVALID_CONFIG"
    assert error["message"].startswith("models/core/broken.yml: invalid YAML: ")
    assert not [row for row in report["errors"] if str(package) in row["message"]]


def test_writes_answer_first_with_three_lines(tmp_path: Path) -> None:
    server, created = _created(tmp_path)
    assert created["outcome"] == [
        "Wrote .gitignore, data/growth_csv/raw_events.csv, examples/core.yml and 5 more.",
        "Parses: 1 entity, 2 measures, 2 metrics; no warnings.",
        f"Revision {created['revision'][:19]}. Next: validate_project mode=runtime before "
        "trusting answers.",
    ]
    stale = _call(
        server,
        "upsert_model",
        {
            "project_path": created["project_path"],
            "model_id": "events",
            "entity_key": "event",
            "relation": "raw_events",
            "primary_key": ["event_id"],
            "expected_revision": "sha256:" + "0" * 64,
            "idempotency_key": "stale",
        },
    )
    assert stale["ok"] is False and stale["error"]["code"] == "CONFIG_CONFLICT"
    assert stale["outcome"][0] == "Refused (CONFIG_CONFLICT); nothing was written."
    assert stale["outcome"][2] == (
        "Next: re-read project_status for the current revision, then retry with a new "
        "idempotency_key."
    )
    preview = _call(
        server,
        "upsert_model",
        {
            "project_path": created["project_path"],
            "model_id": "events",
            "entity_key": "event",
            "relation": "raw_events",
            "primary_key": ["event_id"],
            "measures": {"broken": {"kind": "aggregate", "expr": "", "value_type": "number"}},
            "expected_revision": created["revision"],
            "idempotency_key": "broken",
        },
    )
    assert preview["ok"] is False
    assert len(preview["outcome"]) == 3
    assert preview["outcome"][0].endswith("nothing was written.")


def test_long_warnings_do_not_crowd_out_a_summary() -> None:
    long = [{"code": "LONG", "message": "x" * 600, "details": {}} for _ in range(10)]
    short = [{"code": "SHORT", "message": "short", "details": {}}]

    # The first of each code always shows; the rest while they fit 2,000 characters.
    assert _first_warnings([*long, *short], 10, 2_000) == [0, 1, 2, 10]
    assert _first_warnings([*long, *short], 10) == list(range(9)) + [10]
