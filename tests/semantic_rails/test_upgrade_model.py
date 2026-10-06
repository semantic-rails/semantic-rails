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
    assert key == '["rewrite","package.yaml",["package"]]'
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


def test_planner_batches_shared_alias_edits(tmp_path):
    source = tmp_path / "package.yml"
    source.write_text("defaults: &shared {x: 1, y: 2}\nother: *shared\n")

    def find(current):
        if current.documents["package.yml"]["defaults"]["x"] == 1:
            yield Finding(
                "rewrite",
                "package.yml",
                1,
                ("defaults",),
                "Update shared values",
                (
                    Edit("package.yml", "replace", ("defaults", "x"), value=3),
                    Edit("package.yml", "replace", ("other", "y"), value=4),
                ),
            )

    rule = Rule("rewrite", "1.0", "same_meaning", "Update shared values", find)
    result = plan(PackageFiles(source), (rule,), {})
    assert safe_load(result.files["package.yml"]) == {
        "defaults": {"x": 3, "y": 4},
        "other": {"x": 3, "y": 4},
    }
    assert result.reformatted == ("package.yml",)


@pytest.mark.parametrize("model_id", [None, "orders"])
def test_sibling_choices_apply_independent_answers(tmp_path, model_id):
    source = tmp_path / "package.yml"
    model = {"dimensions": {name: {"legacy": True} for name in ("first", "second", "name")}}
    if model_id is not None:
        model["id"] = model_id
    source.write_text(dump_project_yaml({"models": {"orders": model}}))
    files = PackageFiles(source)

    def find(current):
        for file, path, row in current.dimensions():
            if row.get("legacy"):
                yield Finding(
                    "rewrite",
                    file,
                    1,
                    path,
                    "Choose a replacement",
                    options=tuple(
                        Option(
                            value,
                            value,
                            True,
                            (
                                Edit(file, "delete", (*path, "legacy")),
                                Edit(file, "insert", path, key="expression", value=value),
                            ),
                        )
                        for value in ("left", "right")
                    ),
                )

    rule = Rule("rewrite", "1.0", "retired", "Choose replacements", find)
    pending = plan(files, (rule,), {}).pending
    keys = [files.choice_key(finding) for finding in pending]
    assert len(set(keys)) == 3
    assert all("legacy" not in key for key in keys)
    answers = dict(zip(keys[:2], ("left", "right"), strict=True))
    result = plan(files, (rule,), answers)
    dimensions = safe_load(result.files["package.yml"])["models"]["orders"]["dimensions"]
    assert dimensions == {
        "first": {"expression": "left"},
        "second": {"expression": "right"},
        "name": {"legacy": True},
    }
    assert len(result.choices) == 2 and len(result.pending) == 1


@pytest.mark.parametrize("answered", [False, True])
def test_duplicate_choice_identity_refuses(files, answered):
    rule = _rule("rewrite", "delete", choice=True)
    [finding] = rule.find(files)
    duplicate = Rule("rewrite", "1.0", "retired", "Duplicate", lambda _: (finding, finding))
    choices = {files.choice_key(finding): "rewrite"} if answered else {}
    with pytest.raises(SemanticLayerError, match="Rules 'rewrite' and 'rewrite' conflict") as exc:
        plan(files, (duplicate,), choices)
    assert exc.value.code == "CONFIG_CONFLICT"


def test_pending_choice_survives_changed_line_and_message(files):
    files = PackageFiles(
        files.source,
        contents={"package.yaml": b"package:\n  id: sample\n  legacy: true\n  choose: true\n"},
    )

    def find(current):
        for file, path, row in current.package():
            if "choose" in row:
                line = next(
                    index
                    for index, text in enumerate(current.contents[file].splitlines(), 1)
                    if b"choose:" in text
                )
                yield Finding(
                    "choice",
                    file,
                    line,
                    (*path, "choose"),
                    f"Choice on line {line}",
                    options=(
                        Option("drop", "Drop", True, (Edit(file, "delete", (*path, "choose")),)),
                    ),
                )

    choice = Rule("choice", "1.0", "retired", "Choose", find)
    result = plan(files, (_rule("delete", "delete"), choice), {})
    assert len(result.pending) == 1 and result.pending[0].line == 4
    assert safe_load(result.files["package.yaml"])["package"] == {"id": "sample", "choose": True}
    updated = PackageFiles(files.source, contents=result.files)
    [shifted] = choice.find(updated)
    assert shifted.line == 3 and shifted.message != result.pending[0].message


@pytest.mark.parametrize(
    "names,replace,expected",
    [
        (["a", "b", "c"], False, [{"id": "c"}]),
        (["a", "b"], False, []),
        (["a", "b", "c"], True, [{"id": "kept"}, {"id": "c"}]),
    ],
)
def test_independent_list_edits_keep_original_targets(files, names, replace, expected):
    files = PackageFiles(
        files.source,
        contents={
            "package.yaml": dump_project_yaml(
                {"semantic_policies": [{"id": name} for name in names]}
            ).encode()
        },
    )

    def find(current):
        for file, path, row in current.policies():
            if row["id"] in {"a", "b"}:
                edit = (
                    Edit(file, "replace", path, value={"id": "kept"})
                    if (replace and row["id"] == "b")
                    else Edit(file, "delete", path)
                )
                yield Finding("retire", file, 1, path, "Retire policy", (edit,))

    rule = Rule("retire", "1.0", "retired", "Retire policies", find)
    result = plan(files, (rule,), {})
    assert safe_load(result.files["package.yaml"])["semantic_policies"] == expected


def test_incompatible_list_edit_orders_refuse(files):
    files = PackageFiles(
        files.source, contents={"package.yaml": b"semantic_policies: [{id: a}, {id: b}, {id: c}]\n"}
    )
    first = Finding(
        "outer",
        "package.yaml",
        1,
        ("semantic_policies", 0),
        "Delete outer",
        (
            Edit("package.yaml", "delete", ("semantic_policies", 2)),
            Edit("package.yaml", "delete", ("semantic_policies", 0)),
        ),
    )
    second = Finding(
        "middle",
        "package.yaml",
        1,
        ("semantic_policies", 1),
        "Delete middle",
        (Edit("package.yaml", "delete", ("semantic_policies", 1)),),
    )
    rules = tuple(
        Rule(f.rule, "1.0", "retired", f.message, lambda _, f=f: (f,)) for f in (first, second)
    )
    with pytest.raises(SemanticLayerError, match="outer.*middle") as exc:
        plan(files, rules, {})
    assert exc.value.code == "CONFIG_CONFLICT"


@pytest.mark.parametrize(
    "opaque",
    [
        {"meta": {"expression": {"measure": "metadata_only", "legacy": True}}},
        {"expression": {"kind": "literal", "value": {"measure": "metadata_only", "legacy": True}}},
        {
            "expression": {
                "measure": "real",
                "parameters": {"expression": {"measure": "metadata_only", "legacy": True}},
            }
        },
        {
            "expression": {
                "kind": "value_filter",
                "field": "real",
                "value": {"measure": "metadata_only", "legacy": True},
            }
        },
        {
            "expression": {
                "kind": "aggregate_if",
                "condition": {"kind": "literal", "value": True},
                "value": {"measure": "real"},
                "meta": {"expression": {"measure": "metadata_only", "legacy": True}},
            }
        },
    ],
)
def test_expression_rules_never_see_opaque_data(opaque):
    source = ROOT / "configs/examples/semantic_rails_package_starter.yml"
    files = PackageFiles(source)
    doc = deepcopy(files.documents[source.name])
    doc["metrics"]["revenue_usd"].update(opaque)
    files = PackageFiles(source, contents={source.name: dump_project_yaml(doc).encode()})

    def find(current):
        for file, path, row in current.expressions():
            if row.get("legacy"):
                yield Finding(
                    "expression",
                    file,
                    1,
                    path,
                    "Drop legacy",
                    (Edit(file, "delete", (*path, "legacy")),),
                )

    rule = Rule("expression", "1.0", "same_meaning", "Expression rewrite", find)
    assert plan(files, (rule,), {}).findings == ()
    rows = list(files.expressions())
    assert not any(row.get("measure") == "metadata_only" for _, _, row in rows)
    if "expression" in opaque:
        assert all(path[-1:] != ("revenue_usd",) for _, path, _ in rows)
    if opaque.get("expression", {}).get("kind") == "aggregate_if":
        assert any(row.get("measure") == "real" for _, _, row in rows)


@pytest.mark.parametrize("duplicate", [False, True])
def test_inline_model_identity_matches_loader_mapping_keys(duplicate):
    source = ROOT / "configs/examples/semantic_rails_package_starter.yml"
    files = PackageFiles(source)
    doc = deepcopy(files.documents[source.name])
    for key in ("customers", "orders"):
        doc["models"][key]["id"] = "legacy" if duplicate else f"legacy_{key}"
    data = dump_project_yaml(doc).encode()
    files = PackageFiles(source, contents={source.name: data})
    loaded = _load_package_source(
        str(source), captured=CapturedSource(str(source), False, ((source.name, data),))
    )
    rows = list(files.models())
    assert {path[-1] for _, path, _ in rows} == set(loaded["models"])
    assert len(rows) == 4


@pytest.mark.parametrize(
    "content,iterator",
    [
        ("graph:\n", "graph_entities"),
        ("graph: {entities: null, relationships: null}\n", "relationships"),
        ("models: {orders: {dimensions: null}}\n", "dimensions"),
        ("models: {orders: {times: null}}\n", "times"),
        ("models: {orders: {measures: null, joins: null}}\n", "relationships"),
        ("model:\n", "models"),
    ],
)
def test_null_sections_iterate_empty(tmp_path, content, iterator):
    (tmp_path / "package.yml").write_text("package: {id: sample}\n")
    if content.startswith("model:\n"):
        (tmp_path / "models").mkdir()
        (tmp_path / "models/orders.yml").write_text(content)
    else:
        (tmp_path / "package.yml").write_text(content)
    assert list(getattr(PackageFiles(tmp_path), iterator)()) == []
