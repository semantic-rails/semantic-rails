"""Source iterator coverage and fail-safe upgrade planning with test-only rules."""

from copy import deepcopy
from pathlib import Path

import pytest

from semantic_rails.architect_scaffold import dump_project_yaml
from semantic_rails.config import _load_package_source, load_package_config
from semantic_rails.config_parts.package_loader import normalize_package
from semantic_rails.errors import SemanticLayerError
from semantic_rails.naming import slug
from semantic_rails.package_snapshot import CapturedSource
from semantic_rails.upgrade.model import Edit, Finding, Option, PackageFiles, Rule, _walk, plan
from semantic_rails.upgrade.registry import RULES
from semantic_rails.yaml_loader import safe_load

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = [
    "configs/semantic_rails/jaffle_shop",
    "configs/semantic_rails/tpch_sf1_showcase",
    "comparisons/semantic_layers/semantic_rails/package",
    "tests/integration/correctness/shop",
    "configs/examples/semantic_rails_package_starter.yml",
]


def _normalized_rows(document, iterator):
    if iterator == "graph_entities":
        return list(document["graph"]["entities"].values())
    if iterator == "relationships":
        rows = [
            {**row, "id": row.get("id") or f"relationship.{slug(key)}"}
            for key, row in document["graph"].get("relationships", {}).items()
        ]
        for model_id, model in document["models"].items():
            for key, row in model.get("joins", {}).items():
                rows.append(
                    {**row, "id": row.get("id", f"relationship.{slug(model_id)}_{slug(key)}")}
                )
        return rows
    if iterator in {"dimensions", "times", "measures"}:
        return [
            row for model in document["models"].values() for row in model.get(iterator, {}).values()
        ]
    if iterator == "policies":
        return document.get("semantic_policies", [])
    return list(document.get(iterator, {}).values())


@pytest.mark.parametrize("package", PACKAGES)
def test_iterators_cover_loaded_object_ids(package):
    files = PackageFiles(ROOT / package)
    config = load_package_config(str(files.source))
    for iterator, field in [
        ("graph_entities", "entities"),
        ("relationships", "relationships"),
        ("dimensions", "dimensions"),
        ("times", "temporal_roles"),
        ("measures", "measures"),
        ("metrics", "metric_recipes"),
        ("segments", "segments"),
        ("policies", "semantic_policies"),
    ]:
        documents = deepcopy(files.documents)
        # Mark every source mapping independently of the iterator, so omissions fail.
        collections = {
            "entities",
            "relationships",
            "models",
            "dimensions",
            "times",
            "measures",
            "joins",
            "metrics",
            "segments",
            "semantic_policies",
        }
        for file, doc in documents.items():
            for path, value in _walk(doc):
                if isinstance(value, dict) and (
                    path in {(), ("model",), ("metric",), ("segment",)}
                    or (len(path) > 1 and path[-2] in collections)
                    or (file == "policies.yml" and len(path) == 1)
                ):
                    value["_authored_origin"] = True
        rows = list(getattr(files, iterator)())
        for file, path, row in rows:
            original = files.documents[file]
            value = documents[file]
            for part in path:
                original = original[part]
                value = value[part]
            assert original is row
            value["_iterator_origin"] = True
        marked = tuple((file, dump_project_yaml(doc).encode()) for file, doc in documents.items())
        capture = CapturedSource(str(files.source), files.directory, marked)
        normalized = normalize_package(_load_package_source(str(files.source), captured=capture))
        normalized_rows = _normalized_rows(normalized, iterator)
        observed = {value["id"] for value in normalized_rows if value.get("_iterator_origin")}
        authored = {value["id"] for value in normalized_rows if value.get("_authored_origin")}
        expected = {row.id for row in getattr(config, field)}
        if iterator == "dimensions":
            expected -= {role.dimension for role in config.temporal_roles} - {
                value["id"] for value in normalized_rows
            }
        assert observed == authored
        assert {value["id"] for value in normalized_rows} == expected
    assert {row.get("id") for _, _, row in files.package()} == {config.package.package_id}
    normalized = normalize_package(_load_package_source(str(files.source)))
    models = {
        str(row.get("id") or (path[-1] if path and path[0] == "models" else Path(file).stem))
        for file, path, row in files.models()
    }
    assert models == set(normalized["models"])


@pytest.mark.parametrize("section", ["models", "relations", "metrics", "segments"])
@pytest.mark.parametrize("wrapper", ["plural", "singular", "bare"])
def test_directory_wrapper_paths(tmp_path, section, wrapper):
    (tmp_path / "package.yml").write_text("package: {id: sample}\n")
    (tmp_path / section).mkdir()
    row = {"id": "object", "legacy": True}
    document = (
        {section: {"object": row}}
        if wrapper == "plural"
        else {section[:-1]: row}
        if wrapper == "singular"
        else row
    )
    (tmp_path / section / "item.yaml").write_text(dump_project_yaml(document))
    files = PackageFiles(tmp_path)
    # Relations are captured for rules even though they have no dedicated iterator.
    iterator = files._objects(section)
    [(file, path, value)] = list(iterator)
    assert file == f"{section}/item.yaml" and value == row
    assert (
        path == (section, "object")
        if wrapper == "plural"
        else path == (section[:-1],)
        if wrapper == "singular"
        else path == ()
    )


def test_companions_defaults_policies_and_recursive_expressions(tmp_path):
    source = tmp_path / "single.yaml"
    source.write_text(
        "package: {id: sample}\nmetrics: {total: {expression: {left: {measure: old}, right: {measure: new}}}}\nsegments: {members: {membership: {where: {expression: {measure: old}}}}}\n"
    )
    for section in ("examples", "tests"):
        (tmp_path / section).mkdir()
        (tmp_path / section / "single.yml").write_text(
            f"{section[:-1]}: {{id: single, query: {{version: 2}}}}\n"
        )
        (tmp_path / section / "many.yaml").write_text(
            f"{section}: {{many: {{query: {{version: 2}}}}}}\n"
        )
        (tmp_path / section / "bare.yml").write_text("query: {version: 2}\n")
    files = PackageFiles(source)
    queries = list(files.queries())
    assert len(queries) == 5
    assert {file for file, _, _ in queries} == {
        "single.yaml",
        "examples/single.yml",
        "examples/many.yaml",
        "tests/single.yml",
        "tests/many.yaml",
    }
    expressions = list(files.expressions())
    assert sum(row.get("measure") == "old" for _, _, row in expressions) == 2
    assert all(isinstance(row, dict) for _, _, row in expressions)
    (tmp_path / "package.yml").write_text("package: {id: directory}\n")
    (tmp_path / "defaults.yml").write_text("defaults: {measure: {legacy: true}}\n")
    (tmp_path / "policies.yml").write_text("- {id: policy.test, legacy: true}\n")
    files = PackageFiles(tmp_path)
    assert list(files.defaults()) == [
        ("defaults.yml", ("defaults",), {"measure": {"legacy": True}})
    ]
    assert list(files.policies()) == [("policies.yml", (0,), {"id": "policy.test", "legacy": True})]


def _rule(id, op, key="new", *, choice=False, stop=False):
    def find(files):
        for file, path, row in files.package():
            if "legacy" not in row:
                continue
            edits = (Edit(file, op, (*path, "legacy"), key=key),)
            yield Finding(
                id,
                file,
                1,
                path,
                "Rewrite legacy property",
                edits=() if choice or stop else edits,
                options=(Option("rewrite", "Rewrite it", False, edits),) if choice else (),
            )

    return Rule(id, "1.0", "same_meaning", "Rewrite legacy property", find)


@pytest.fixture
def files(tmp_path):
    source = tmp_path / "package.yaml"
    source.write_text("# retained\npackage:\n  id: sample\n  legacy: true # old\n")
    return PackageFiles(source)


@pytest.mark.parametrize("operation", ["rename", "delete"])
def test_mechanical_plan_and_idempotence(files, operation):
    rule = _rule("rewrite", operation)
    result = plan(files, (rule,), {})
    assert not result.pending and len(result.findings) == 1 and not result.reformatted
    assert result.files["package.yaml"].startswith(b"# retained\n")
    row = safe_load(result.files["package.yaml"])["package"]
    assert "legacy" not in row and ("new" in row) is (operation == "rename")
    updated = PackageFiles(files.source, contents=result.files)
    assert plan(updated, (rule,), {}).files == {}


@pytest.mark.parametrize("stop", [False, True])
def test_choice_pending_and_answered(files, stop):
    rule = _rule("rewrite", "delete", choice=not stop, stop=stop)
    result = plan(files, (rule,), {})
    assert len(result.pending) == 1 and result.files == {}
    key = files.choice_key(result.pending[0])
    assert key == "rewrite:sample"
    if stop:
        with pytest.raises(SemanticLayerError):
            plan(files, (rule,), {key: "rewrite"})
    else:
        result = plan(files, (rule,), {key: "rewrite"})
        assert (
            not result.pending
            and "legacy" not in safe_load(result.files["package.yaml"])["package"]
        )
        assert result.choices[key].changes_answers is False
    with pytest.raises(SemanticLayerError):
        plan(files, (rule,), {key: "unknown"})


def test_unknown_choice_refuses(files):
    with pytest.raises(SemanticLayerError, match="Unknown upgrade choice"):
        plan(files, (), {"unknown": "rewrite"})


def test_non_idempotent_rule_refuses(files):
    rule = _rule("loop", "rename", key="legacy_again")

    def always_find(current):
        for file, path, row in current.package():
            key = "legacy" if "legacy" in row else "legacy_again"
            yield Finding(
                "loop",
                file,
                1,
                path,
                "Always matches",
                (Edit(file, "replace", (*path, key), value=True),),
            )

    rule = Rule(rule.id, rule.since, rule.effect, rule.summary, always_find)
    with pytest.raises(SemanticLayerError, match="rule 'loop' is not idempotent") as exc:
        plan(files, (rule,), {})
    assert exc.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("second", ["rename", "delete"])
def test_conflicting_rules_refuse(files, second):
    with pytest.raises(SemanticLayerError, match="Rules 'first' and 'second' conflict") as exc:
        plan(files, (_rule("first", "rename"), _rule("second", second)), {})
    assert exc.value.code == "CONFIG_CONFLICT"


def test_new_cross_rule_finding_is_not_idempotent(files):
    def find_new(current):
        for file, path, row in current.package():
            if "new" in row:
                yield Finding(
                    "second", file, 1, path, "Found new", (Edit(file, "delete", (*path, "new")),)
                )

    second = Rule("second", "1.0", "drops", "Delete new", find_new)
    with pytest.raises(SemanticLayerError, match="rule 'second' is not idempotent"):
        plan(files, (_rule("first", "rename"), second), {})


def test_file_creation_and_archival_are_virtual(files):
    original = files.source.read_bytes()

    def find(current):
        if "package.yaml" in current.documents:
            yield Finding(
                "move",
                "package.yaml",
                1,
                (),
                "Move file",
                (
                    Edit("package.yaml", "archive"),
                    Edit("new.yaml", "create", value={"package": {"id": "sample"}}),
                ),
            )

    rule = Rule("move", "1.0", "same_meaning", "Move file", find)
    result = plan(files, (rule,), {})
    assert result.files["package.yaml"] is None
    assert safe_load(result.files["new.yaml"]) == {"package": {"id": "sample"}}
    assert files.source.read_bytes() == original and not (files.root / "new.yaml").exists()
    assert RULES == ()


def test_loader_precedence_and_unrelated_sections(tmp_path):
    (tmp_path / "package.yml").write_text(
        "package: {id: sample}\ngraph: {entities: {old: {id: old}}}\nmodels: {item: {id: item, legacy: false}}\n"
    )
    (tmp_path / "graph.yml").write_text(
        "graph: {entities: {new: {id: new}}}\nmetrics: {ignored: {id: ignored}}\n"
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "item.yml").write_text("model: {id: item, legacy: true}\n")
    files = PackageFiles(tmp_path)
    assert [row["id"] for _, _, row in files.graph_entities()] == ["new"]
    assert list(files.models()) == [("models/item.yml", ("model",), {"id": "item", "legacy": True})]
    assert list(files.metrics()) == []


def test_direct_metric_expressions_are_recursive(tmp_path):
    source = tmp_path / "package.yml"
    source.write_text(
        "metrics: {ratio: {kind: ratio, numerator: {measure: old}, denominator: {measure: new}, meta: {measure: ignored}}}\n"
    )
    rows = list(PackageFiles(source).expressions())
    assert {row["measure"] for _, _, row in rows if "measure" in row} == {"old", "new"}


def test_one_finding_can_apply_sequential_edits(files):
    def find(current):
        for file, path, row in current.package():
            if "legacy" in row:
                yield Finding(
                    "rewrite",
                    file,
                    1,
                    path,
                    "Rename and replace",
                    (
                        Edit(file, "rename", (*path, "legacy"), key="new"),
                        Edit(file, "replace", (*path, "new"), value=False),
                    ),
                )

    rule = Rule("rewrite", "1.0", "same_meaning", "Rewrite legacy", find)
    assert safe_load(plan(files, (rule,), {}).files["package.yaml"])["package"]["new"] is False


@pytest.mark.parametrize(
    "path,op,key", [(("package",), "replace", ""), (("package",), "insert", "new")]
)
def test_overlapping_paths_and_rename_destination_refuse(files, path, op, key):
    def find(current):
        yield Finding(
            "second",
            "package.yaml",
            1,
            path,
            "Overlap",
            (Edit("package.yaml", op, path, key=key, value={}),),
        )

    rule = Rule("second", "1.0", "same_meaning", "Overlap", find)
    with pytest.raises(SemanticLayerError, match="Rules 'first' and 'second' conflict"):
        plan(files, (_rule("first", "rename"), rule), {})


def test_duplicate_finding_emission_conflicts(files):
    finding = Finding(
        "rewrite",
        "package.yaml",
        1,
        ("package",),
        "Replace legacy",
        (Edit("package.yaml", "replace", ("package", "legacy"), value=False),),
    )

    def find(current):
        if current.documents["package.yaml"]["package"]["legacy"]:
            return (finding, finding)
        return ()

    rule = Rule("rewrite", "1.0", "same_meaning", "Replace legacy", find)
    with pytest.raises(SemanticLayerError, match="Rules 'rewrite' and 'rewrite' conflict") as exc:
        plan(files, (rule,), {})
    assert exc.value.code == "CONFIG_CONFLICT"
