"""Keys the loader never read are deleted when it reads the same value elsewhere, else asked."""

import pytest
import yaml

from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.upgrade.model import PackageFiles, plan
from semantic_rails.upgrade.rules_strict import RULES
from semantic_rails.yaml_loader import safe_load
from tests.semantic_rails.conftest import write_single_file_package


def _scope(package=None, defaults=None, block=True):
    def edit(doc):
        if package is not None:
            doc["package"]["observation_scope"] = package
        if not block:
            del doc["defaults"]
        elif defaults is not None:
            doc["defaults"]["observation_scope"] = defaults

    return edit


def _channel(**keys):
    def edit(doc):
        doc["models"]["orders"]["dimensions"]["channel"].update(keys)

    return edit


# legacy edit, author choice (None: the rewrite is mechanical), current edit
CASES = {
    "scope-equals-defaults": (_scope("query", "query"), None, _scope(None, "query")),
    "scope-equals-default": (_scope("dataset"), None, _scope()),
    "scope-differs-delete": (_scope("query"), "delete", _scope()),
    "scope-differs-use": (_scope("query"), "use", _scope(None, "query")),
    "scope-replaces-default": (_scope("query", "dataset"), "use", _scope(None, "query")),
    "scope-without-defaults": (
        _scope("query", block=False),
        "use",
        lambda doc: doc.__setitem__("defaults", {"observation_scope": "query"}),
    ),
    "expr-equals-key": (_channel(expr="channel"), None, _channel()),
    "expr-equals-column": (
        _channel(column="sales_channel", expr="sales_channel"),
        None,
        _channel(column="sales_channel"),
    ),
    "expr-differs-delete": (_channel(expr="sales_channel"), "delete", _channel()),
    "expr-differs-use": (_channel(expr="sales_channel"), "use", _channel(column="sales_channel")),
    "expr-replaces-column": (
        _channel(column="channel", expr="sales_channel"),
        "use",
        _channel(column="sales_channel"),
    ),
}


def _write(root, edit):
    source = write_single_file_package(root / "project")
    doc = safe_load(source.read_bytes())
    edit(doc)
    source.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return source


@pytest.mark.parametrize(("legacy", "choice", "current"), CASES.values(), ids=CASES)
def test_ignored_key_golden_rewrite(tmp_path, legacy, choice, current):
    source = _write(tmp_path / "legacy", legacy)
    files = PackageFiles(source)
    result = plan(files, RULES, {})
    assert len(result.findings) == 1
    if choice is None:
        assert not result.pending
    else:
        (finding,) = result.pending
        assert [(option.id, option.changes_answers) for option in finding.options] == [
            ("delete", False),
            ("use", True),
        ]
        result = plan(files, RULES, {files.choice_key(finding): choice})
    expected = _write(tmp_path / "current", current)
    assert safe_load(result.files[source.name]) == safe_load(expected.read_bytes())
    upgraded = PackageFiles(source, contents={**files.contents, **result.files})
    assert not plan(upgraded, RULES, {}).findings
    source.write_bytes(result.files[source.name])
    assert (
        load_package_snapshot(source).semantic_fingerprint
        == load_package_snapshot(expected).semantic_fingerprint
    )


def test_ignored_key_edit_preserves_comments(tmp_path):
    source = tmp_path / "pkg.yml"
    source.write_text(
        "package:\n  id: pkg\n  # The writer's copy.\n  observation_scope: query  # ignored\n"
        "defaults:\n  observation_scope: query  # Read here.\n"
    )
    result = plan(PackageFiles(source), RULES, {})
    assert result.files[source.name].decode() == source.read_text().replace(
        "  observation_scope: query  # ignored\n", ""
    )
