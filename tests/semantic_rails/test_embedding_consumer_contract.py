"""The ``semantic_rails.embedding`` uses recorded from a downstream embedder keep working.

``fixtures/embedding_consumer_uses.txt`` lists what a hosted embedder uses, generated from
its code by ``scripts/embedding_consumer_contract.py``: names, attributes, the argument
shapes of calls whose receiver the scan can place, and the exact members and parameters of
the protocols it implements. A failure means this change would break that embedder when it
upgrades. Keep the old form working next to the new one and deprecate it (docs/EMBEDDING.md,
"Changing the facade"); regenerate the list only once the embedder has stopped using it.

The facade reference in docs/EMBEDDING.md lists every exported name with its call shape, so a
new export is documented and a changed signature shows up in the docs diff.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import semantic_rails.embedding as embedding
from scripts import embedding_consumer_contract as contract
from scripts.embedding_consumer_contract import (
    HEADER,
    REPO_ROOT,
    USES_FILE,
    _parameters,
    _protocol_shape,
    problem,
    scan,
)

USES = USES_FILE.read_text(encoding="utf-8").removeprefix(HEADER).splitlines()
EMBEDDING_DOC = REPO_ROOT / "docs" / "EMBEDDING.md"


@pytest.fixture(autouse=True)
def recorded_uses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "recorded.txt"
    path.write_text(HEADER, encoding="utf-8")
    monkeypatch.setattr(contract, "USES_FILE", path)
    return path


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
        ("emit_audit_event().anything", "emit_audit_event is no longer a class"),
        ("AuditSink{emit(self, payload)}", None),
        ("AuditSink{emit(self)}", "AuditSink is now {emit(self, payload)}"),
        (
            "PolicyContextResolver{resolve(self, headers, *, payload=)}",
            "PolicyContextResolver is now {resolve(self, headers, *, payload=, request_id=)}",
        ),
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
from semantic_rails.embedding import Runtime, SemanticHTTPService as Service

def route(entry, request):
    host.runtime.helpers.ignored()
    return entry.service.handle("POST", request), entry.runtime.not_an_engine_attribute

def typed_route(service: Service, runtime: Runtime):
    return service.handle("POST", request), runtime.not_an_engine_attribute
""",
    "tests/test_host.py": """
from unittest import mock

import semantic_rails.embedding as engine
from semantic_rails import embedding as facade

def test_it(monkeypatch, args, sink):
    monkeypatch.setattr(engine, "Runtime", object)
    monkeypatch.setattr("semantic_rails.embedding.emit_audit_event", print)
    engine.RequestContext(*args, request_id="r")
    facade.set_audit_sink(sink)
    with mock.patch.object(facade.Runtime, "close"):
        context: facade.RequestContext = facade.RequestContext(request_id="r")
        context.to_policy_context()
""",
}


def test_scan_follows_imports_instances_and_patches(tmp_path: Path) -> None:
    for name, source in CONSUMER.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(source, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=120)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, timeout=120)

    kept, failing = scan(tmp_path)

    assert kept == [
        "AuditSink{emit(self, payload)}",  # set_audit_sink takes one
        "RequestContext().to_policy_context()",
        "RequestContext(*, request_id=)",
        "RequestContext(request_id=)",
        "Runtime().package_id",
        "Runtime().set_adapter(_)",
        "Runtime.close",
        "Runtime.from_path(_)",
        "SemanticHTTPService().handle(_, _)",
        "SemanticHTTPService(_, package_id=)",
        "emit_audit_event",
        "set_audit_sink(_)",
    ]
    assert list(failing) == ["Runtime().not_an_engine_attribute"]


@pytest.mark.parametrize(
    ("source", "unexpected"),
    [
        ("entry.result.all()", "WarehouseAdapter().all"),
        ("entry.result.one_or_none()", "WarehouseAdapter().one_or_none"),
        ("entry.client.auto_paging_iter()", "WarehouseAdapter().auto_paging_iter"),
        ("entry.client.v1", "WarehouseAdapter().v1"),
        *[
            (f"runtime = FakeRuntime(); runtime.{member}", f"Runtime().{member}")
            for member in ("blockers", "configured", "generation", "path", "set_calls")
        ],
        ("class Runtime: pass\nRuntime()", "Runtime()"),
    ],
)
def test_scan_does_not_attribute_other_objects_or_test_doubles(
    tmp_path: Path, source: str, unexpected: str
) -> None:
    files = {
        "host.py": """
from semantic_rails.embedding import Runtime, create_warehouse_adapter

def build():
    runtime = Runtime.from_path("package")
    result = create_warehouse_adapter("package")
    client = create_warehouse_adapter("package")
    entry.runtime = runtime
    entry.result = result
    entry.client = client
""",
        "tests/test_host.py": f"""
from semantic_rails.embedding import RequestContext

def real():
    from semantic_rails.embedding import Runtime
    runtime = Runtime.from_path("package")
    runtime.close()

{source}
""",
    }
    kept, failing = _scan_sources(tmp_path, files)
    assert "Runtime().close()" in kept
    assert unexpected not in kept and unexpected not in failing
    assert failing == {}


def _scan_sources(tmp_path: Path, files: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=120)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, timeout=120)
    return scan(tmp_path)


@pytest.mark.parametrize(
    ("recorded", "source", "expected", "retained"),
    [
        ("Runtime().adapter", "entry.runtime.adapter = None", ["Runtime().adapter"], True),
        (
            "SemanticHTTPService().exception_payload(_)",
            "svc.exception_payload(exc)",
            ["SemanticHTTPService().exception_payload(_)"],
            True,
        ),
        (
            "SemanticHTTPService().exception_payload(_, stage=)",
            "svc.exception_payload(exc, stage='query')",
            ["SemanticHTTPService().exception_payload(_, stage=)"],
            True,
        ),
        ("Runtime().package_id", "entry.runtime.adapter = None", [], False),
        ("", "runtime.blockers", [], False),
        ("dialect_for_warehouse(_)", "entry.other_helper(value)", [], False),
        (
            "dialect_for_warehouse(_)",
            "entry.dialect_for_warehouse(value)",
            ["dialect_for_warehouse(_)"],
            True,
        ),
        ("Runtime().adapter", "entry.adapter: object = None", ["Runtime().adapter"], True),
        ("Runtime().adapter", "for entry.adapter in values: pass", ["Runtime().adapter"], True),
        (
            "Runtime().adapter",
            "with external() as entry.adapter: pass",
            ["Runtime().adapter"],
            True,
        ),
        (
            "Runtime().adapter",
            "from semantic_rails.embedding import Runtime\n"
            'runtime = Runtime.from_path("package")\nruntime.adapter = None',
            ["Runtime().adapter", "Runtime.from_path(_)"],
            True,
        ),
        (
            "Runtime().adapter",
            "from semantic_rails.embedding import Runtime\n"
            "runtime: Runtime = external()\nruntime.adapter = None",
            ["Runtime().adapter"],
            True,
        ),
        (
            "RequestContext(tenant=)",
            "from semantic_rails.embedding import RequestContext\n"
            "def build():\n    [None for RequestContext in ()]\n"
            '    return RequestContext(tenant="t")',
            ["RequestContext(tenant=)"],
            True,
        ),
        *[
            (
                "RequestContext(tenant=)",
                "from semantic_rails.embedding import RequestContext\n"
                f'callback = lambda {prefix}context=RequestContext(tenant="t"): context',
                ["RequestContext(tenant=)"],
                True,
            )
            for prefix in ("", "*, ")
        ],
        (
            "RequestContext(tenant=)",
            'def build():\n    return RequestContext(tenant="t")\n\n'
            "from semantic_rails.embedding import RequestContext\nbuild()",
            ["RequestContext(tenant=)"],
            True,
        ),
        ("RequestContext(tenant=)", "context = external()", [], False),
        (
            "RequestContext(tenant=)",
            "RequestContext = external()",
            ["RequestContext(tenant=)"],
            True,
        ),
        ("RequestContext(tenant=)", "del RequestContext", ["RequestContext(tenant=)"], True),
        (
            "RequestContext(tenant=)",
            "from other_library import RequestContext as Context",
            ["RequestContext(tenant=)"],
            True,
        ),
        ("Runtime.from_path(_)", "del entry.from_path", ["Runtime.from_path(_)"], True),
        ("RequestContext", "entry.RequestContext", ["RequestContext"], True),
        (
            "AuditSink{emit(self, payload)}",
            "from other_library import AuditSink as Sink",
            ["AuditSink{emit(self, payload)}"],
            True,
        ),
        ("RequestContext(tenant=)", 'label = "RequestContext"\n# RequestContext', [], False),
    ],
)
def test_scan_requires_proof_to_add_or_remove_instance_uses(
    tmp_path: Path,
    recorded_uses: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    recorded: str,
    source: str,
    expected: list[str],
    retained: bool,
) -> None:
    if recorded == "SemanticHTTPService().exception_payload(_)":
        # Isolate retention from the current method's required stage keyword.
        monkeypatch.setattr(
            embedding.SemanticHTTPService, "exception_payload", lambda self, exc: None
        )
    recorded_uses.write_text(HEADER + recorded + "\n", encoding="utf-8")
    kept, failing = _scan_sources(tmp_path, {"host.py": source})
    assert kept == expected
    assert failing == {}
    assert capsys.readouterr().err == (
        f"retained (name still present): {recorded}\n" if retained else ""
    )
    recorded_uses.write_text(HEADER + "".join(f"{use}\n" for use in kept), encoding="utf-8")
    assert contract.main(["--consumer", str(tmp_path), "--check"]) == 0
    assert capsys.readouterr().out == ""


def test_retained_use_still_checks_engine_compatibility(
    tmp_path: Path, recorded_uses: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded_uses.write_text(HEADER + "Runtime().missing_member(_)\n", encoding="utf-8")
    _scan_sources(tmp_path, {"host.py": "entry.missing_member(value)"})
    assert contract.main(["--consumer", str(tmp_path)]) == 0
    assert "not recorded: Runtime().missing_member(_):" in capsys.readouterr().err
    assert recorded_uses.read_text(encoding="utf-8") == HEADER


def test_scan_retains_members_from_other_tracked_files_only(
    tmp_path: Path, recorded_uses: Path
) -> None:
    recorded_uses.write_text(HEADER + "Runtime().adapter\nRuntime().package_id\n", encoding="utf-8")
    _scan_sources(tmp_path, {"host.py": "pass", "tests/test_host.py": "entry.adapter"})
    (tmp_path / "untracked.py").write_text("entry.package_id", encoding="utf-8")
    kept, failing = scan(tmp_path)
    assert kept == ["Runtime().adapter"]
    assert failing == {}


@pytest.mark.parametrize(
    "source",
    [
        "runtime: Runtime\nruntime.close()",
        "runtime: Runtime = external()\nruntime.close()",
        "runtime: Runtime | None\nruntime.close()",
        "def build() -> Runtime: ...\nruntime = build()\nruntime.close()",
        "def route(runtime: Runtime):\n    runtime.close()",
        "def route(runtime: 'Runtime'):\n    runtime.close()",
        "runtime = Runtime.from_path('package')\nruntime.close()",
        "Runtime.from_path('package').close()",
        "runtime = Runtime.from_path('package')\nalias = runtime\nalias.close()",
        "runtime = Runtime.from_path('package')\nentry.runtime = runtime\nentry.runtime.close()",
    ],
)
def test_scan_records_proven_instances(tmp_path: Path, source: str) -> None:
    kept, failing = _scan_sources(
        tmp_path, {"host.py": "from semantic_rails.embedding import Runtime\n" + source}
    )
    assert "Runtime().close()" in kept
    assert failing == {}


@pytest.mark.parametrize(
    "source",
    [
        "runtime = Runtime.from_path('package')\nruntime = external()\nruntime.close()",
        "runtime = Runtime.from_path('package')\ndef route(runtime):\n    runtime.close()",
        "def first():\n    runtime = Runtime.from_path('package')\ndef other(runtime):\n    runtime.close()",
        "def route():\n    Runtime()\n    class Runtime: pass",
        "Runtime = external\nRuntime()",
        "from other_library import Runtime\nRuntime()",
        "entry.runtime = Runtime.from_path('package')\nother.runtime.close()",
        "entry.runtime = Runtime.from_path('package')\nentry = external()\nentry.runtime.close()",
        "entry.runtime = Runtime.from_path('package')\ndef route(entry):\n    entry.runtime.close()",
        "runtime = Runtime.from_path('package')\nif flag:\n    runtime = external()\nruntime.close()",
        "runtime = external()\nif flag:\n    runtime = Runtime.from_path('package')\nruntime.close()",
        "runtime = Runtime.from_path('package')\nfor runtime in values:\n    runtime.close()",
        "runtime = Runtime.from_path('package')\nwith external() as runtime:\n    runtime.close()",
        "runtime = Runtime.from_path('package')\ncallback = lambda runtime: runtime.close()",
    ],
)
def test_scan_forgets_shadowed_or_unrelated_bindings(tmp_path: Path, source: str) -> None:
    kept, failing = _scan_sources(
        tmp_path, {"host.py": "from semantic_rails.embedding import Runtime\n" + source}
    )
    assert "Runtime().close()" not in kept
    assert "Runtime()" not in failing


def test_scan_records_annotation_only_protocol_uses(tmp_path: Path) -> None:
    kept, failing = _scan_sources(
        tmp_path,
        {
            "host.py": "from semantic_rails.embedding import AuditSink\ndef route(sink: AuditSink): pass"
        },
    )
    assert kept == ["AuditSink{emit(self, payload)}"]
    assert failing == {}


def _reference(name: str) -> str:
    value = getattr(embedding, name)
    if (shape := _protocol_shape(value)) is not None:
        return f"{name}{{{shape}}}"
    return f"{name}({_parameters(value)})" if callable(value) else name


def test_every_export_imports_and_matches_the_documented_reference() -> None:
    namespace: dict[str, object] = {}
    exec("from semantic_rails.embedding import *", namespace)
    assert set(embedding.__all__) <= set(namespace)
    assert len(set(embedding.__all__)) == len(embedding.__all__)
    section = EMBEDDING_DOC.read_text(encoding="utf-8").split("## Facade reference\n", 1)[1]
    documented = section.split("```text\n", 1)[1].split("```", 1)[0].splitlines()
    expected = [_reference(name) for name in sorted(embedding.__all__)]
    assert documented == expected, "update docs/EMBEDDING.md's facade reference to:\n" + "\n".join(
        expected
    )
