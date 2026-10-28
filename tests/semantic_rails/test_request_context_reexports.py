"""Old import paths for the audit names that moved out of ``semantic_rails.request_context``.

The audit sink moved to ``semantic_rails.audit``. ``request_context`` re-exports it, as the
same objects, until ``REMOVED_IN``; the embedding facade keeps exporting the audit names for
good. The API-key helpers live only in ``semantic_rails.api_keys``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from semantic_rails import audit, embedding, request_context

REMOVED_IN = (0, 3, 3)
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
MOVED = {
    audit: (
        "AuditSink",
        "StderrAuditSink",
        "audit_logging_enabled",
        "emit_audit_event",
        "get_audit_sink",
        "set_audit_sink",
    ),
}


@pytest.mark.parametrize(
    ("module", "name"),
    [pytest.param(module, name, id=name) for module, names in MOVED.items() for name in names],
)
def test_old_import_paths_give_the_moved_objects(module, name) -> None:
    assert getattr(request_context, name) is getattr(module, name)
    assert name in embedding.__all__
    assert getattr(embedding, name) is getattr(module, name)


def test_remove_request_context_reexports_before_releasing_0_3_3() -> None:
    version = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    assert match, version
    assert tuple(map(int, match.groups())) < REMOVED_IN, (
        f"Version {version} is due to drop the old request_context import paths: delete the "
        "audit re-export block from semantic_rails/request_context.py and this test file, and "
        "add a `removed` changelog fragment."
    )
