"""Every ``semantic_rails.embedding`` use a downstream embedder relies on keeps working.

``fixtures/embedding_consumer_uses.txt`` lists the names, attributes and call shapes
a hosted embedder uses, generated from its code by
``scripts/embedding_consumer_contract.py``. A failure means this change would break that
embedder when it upgrades. Keep the old form working next to the new one and deprecate it
(docs/EMBEDDING.md, "Changing the facade"); regenerate the list only once the embedder
has stopped using it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.embedding_consumer_contract import HEADER, USES_FILE, problem, scan

USES = USES_FILE.read_text(encoding="utf-8").removeprefix(HEADER).splitlines()


@pytest.mark.parametrize("use", USES)
def test_embedder_use_still_works(use: str) -> None:
    assert problem(use) is None, (
        f"{use}: {problem(use)}. A downstream embedder depends on this; see docs/EMBEDDING.md."
    )


def test_uses_file_is_canonical() -> None:
    assert sorted(set(USES)) == USES and all(USES), "regenerate the file with its script"


@pytest.mark.parametrize(
    ("use", "reason"),
    [
        ("Runtime().set_adapter(_)", None),  # the instance supplies self
        ("WarehouseAdapter.query_prepared(_, _, limits=)", None),  # unbound: self is passed
        ("Runtime().adapter", None),  # assigned in a method, not a class attribute
        ("SemanticHTTPService(**)", None),  # unpacked arguments bind partially
        ("NotExported", "no longer exports NotExported"),
        ("Runtime.no_such_attribute", "has no attribute 'no_such_attribute'"),
        ("Runtime().no_such_attribute", "instances have no attribute 'no_such_attribute'"),
        ("Runtime.from_path(_, _)", "no longer accepts this call"),
        ("Runtime().set_adapter()", "no longer accepts this call"),
        ("SemanticHTTPService(_, no_such_keyword=)", "no longer accepts this call"),
        ("Runtime()", "missing a required argument: 'package_id'"),
    ],
)
def test_problem_reports_only_breaking_uses(use: str, reason: str | None) -> None:
    found = problem(use)
    assert found is None if reason is None else reason in (found or ""), found


CONSUMER = {
    "host/service.py": """
from semantic_rails.embedding import Runtime, SemanticHTTPService as Service

def build(path, adapter):
    runtime = Runtime.from_path(path)
    runtime.set_adapter(adapter)
    service = Service(runtime, package_id=runtime.package_id)
    return Entry(service=service, runtime=runtime)
""",
    "host/routes.py": """
import host.runtime

def route(entry, request):
    host.runtime.helpers.ignored()
    return entry.service.handle("POST", request), entry.runtime.not_an_engine_attribute
""",
    "tests/test_host.py": """
import semantic_rails.embedding as engine

def test_it(monkeypatch, args):
    monkeypatch.setattr(engine, "Runtime", object)
    monkeypatch.setattr("semantic_rails.embedding.emit_audit_event", print)
    engine.RequestContext(*args, request_id="r")
""",
}


def test_scan_follows_imports_instances_and_patches(tmp_path: Path) -> None:
    for name, source in CONSUMER.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(source, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)

    kept, failing = scan(tmp_path)

    assert kept == [
        "RequestContext(*, request_id=)",
        "Runtime().package_id",
        "Runtime().set_adapter(_)",
        "Runtime.from_path(_)",
        "SemanticHTTPService().handle(_, _)",
        "SemanticHTTPService(_, package_id=)",
        "emit_audit_event",
    ]
    assert list(failing) == ["Runtime().not_an_engine_attribute"]
