"""YAML editing equivalence and byte preservation over authored package sources."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from semantic_rails.errors import SemanticLayerError
from semantic_rails.upgrade.edits import apply_edits
from semantic_rails.upgrade.model import Edit
from semantic_rails.yaml_loader import Yaml12SafeLoader, safe_load

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PATHS = (
    "configs/semantic_rails/jaffle_shop",
    "configs/semantic_rails/tpch_sf1_showcase",
    "comparisons/semantic_layers/semantic_rails/package",
    "tests/integration/correctness/shop",
    "configs/examples/semantic_rails_package_starter.yml",
)
YAML_FILES = sorted(
    {
        file
        for name in PACKAGE_PATHS
        for file in ([ROOT / name] if (ROOT / name).is_file() else (ROOT / name).rglob("*.yml"))
    }
)


@pytest.mark.parametrize(
    "text,edit,expected,reformatted",
    [
        (
            "# before\na: old # trailing\n# between\nb: 'yes' # after\n",
            Edit("", "replace", ("a",), value="on"),
            {"a": "on", "b": "yes"},
            False,
        ),
        ("a: old\nb: 2\n", Edit("", "rename", ("a",), key="no"), {"no": "old", "b": 2}, False),
        (
            "# above\na: old # remove\n# below\nb: 2 # after\n",
            Edit("", "delete", ("a",)),
            {"b": 2},
            False,
        ),
        (
            "a:\n  x: 1\n  # internal\n  y: 2\n# sibling\nb: 3\n",
            Edit("", "delete", ("a",)),
            {"b": 3},
            False,
        ),
        (
            "a: 1 # inline\n# between\nb: 2\n# after\n",
            Edit("", "insert", (), key="c", value="yes", after="a"),
            {"a": 1, "b": 2, "c": "yes"},
            False,
        ),
        (
            "outer:\n  a: 1\n# after\n",
            Edit("", "insert", ("outer",), key="b", value={"x": ["no", "on"]}),
            {"outer": {"a": 1, "b": {"x": ["no", "on"]}}},
            False,
        ),
        ("a: 1", Edit("", "insert", (), key="b", value=2), {"a": 1, "b": 2}, False),
        (
            "a: 1\r\nb: 2\r\n",
            Edit("", "insert", (), key="c", value=3),
            {"a": 1, "b": 2, "c": 3},
            False,
        ),
        (
            "# before\nx: {a: old, b: 2} # after\n",
            Edit("", "replace", ("x", "a"), value="yes"),
            {"x": {"a": "yes", "b": 2}},
            False,
        ),
        (
            "x: {a: old, b: 2}\n",
            Edit("", "rename", ("x", "a"), key="on"),
            {"x": {"on": "old", "b": 2}},
            False,
        ),
        (
            "# before\nx: {a: old, b: 2} # after\n",
            Edit("", "delete", ("x", "a")),
            {"x": {"b": 2}},
            True,
        ),
        (
            "# before\nx: {a: old, b: 2} # after\n",
            Edit("", "insert", ("x",), key="c", value="no"),
            {"x": {"a": "old", "b": 2, "c": "no"}},
            True,
        ),
        ("x: [1, 2, 3] # after\n", Edit("", "delete", ("x", 1)), {"x": [1, 3]}, True),
        ("x: [1, 2] # after\n", Edit("", "replace", ("x",), value={"a": 3}), {"x": {"a": 3}}, True),
        ("a: 1 # only\n", Edit("", "delete", ("a",)), {}, True),
        (
            "a: &value old\nb: *value\n",
            Edit("", "replace", ("a",), value="new"),
            {"a": "new", "b": "old"},
            True,
        ),
        (
            "a: &obj {x: old}\nb: *obj\n",
            Edit("", "replace", ("b", "x"), value="new"),
            {"a": {"x": "new"}, "b": {"x": "new"}},
            True,
        ),
        (
            "items:\n- a: 1\n  b: 2\n",
            Edit("", "delete", ("items", 0, "a")),
            {"items": [{"b": 2}]},
            True,
        ),
        (
            "a: |\n  hello\n  world\nb: 2\n",
            Edit("", "replace", ("a",), value="new"),
            {"a": "new", "b": 2},
            True,
        ),
    ],
)
def test_edit_operations(text, edit, expected, reformatted):
    result, changed = apply_edits(text, [edit])
    assert safe_load(result) == expected
    assert changed is reformatted
    if "# before" in text:
        assert result.startswith("# before\n")
    if "# after" in text:
        assert "# after" in result
    if "# between" in text:
        assert "# between" in result
    if "# sibling" in text:
        assert "# sibling" in result
    if "# above" in text:
        assert "# above" in result and "# below" in result and "# remove" not in result
    if edit.key in {"on", "no", "yes"} or edit.value in ("on", "no", "yes"):
        assert yaml.safe_load(result) == expected


@pytest.mark.parametrize(
    "edit",
    [
        Edit("", "rename", ("a",), key="b"),
        Edit("", "insert", (), key="a", value=3),
        Edit("", "delete", ("missing",)),
        Edit("", "insert", (), key="c", value=3, after="missing"),
    ],
)
def test_invalid_edits_refuse(edit):
    with pytest.raises(SemanticLayerError) as exc:
        apply_edits("a: 1\nb: 2\n", [edit])
    assert exc.value.code == "INVALID_CONFIG"


def test_sequential_edits_and_file_level_passthrough():
    text = "a: 1\nb: 2\n"
    result, reformatted = apply_edits(
        text,
        [
            Edit("", "rename", ("a",), key="c"),
            Edit("", "replace", ("c",), value=3),
            Edit("", "archive"),
            Edit("", "create", value="new"),
        ],
    )
    assert result == "c: 3\nb: 2\n" and not reformatted


def _leaves(node, text, path=(), seen=None):
    if seen is None:
        seen = set()
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, MappingNode):
        for key, value in node.value:
            if key.tag == "tag:yaml.org,2002:merge":
                continue  # Merge sources are visited at their anchor declaration.
            name = safe_load(text[key.start_mark.index : key.end_mark.index])
            yield from _leaves(value, text, (*path, name), seen)
    elif isinstance(node, SequenceNode):
        for index, value in enumerate(node.value):
            yield from _leaves(value, text, (*path, index), seen)
    elif isinstance(node, ScalarNode):
        yield path, node


@pytest.mark.parametrize(
    "file,part,parts",
    [
        (file, part, 4 if file.stat().st_size > 100_000 else 1)
        for file in YAML_FILES
        for part in range(4 if file.stat().st_size > 100_000 else 1)
    ],
    ids=[
        f"{file.relative_to(ROOT)}-{part}"
        for file in YAML_FILES
        for part in range(4 if file.stat().st_size > 100_000 else 1)
    ],
)
def test_bundled_yaml_edit_properties(file, part, parts):
    text = file.read_text()
    original = safe_load(text)
    root = yaml.compose(text, Loader=Yaml12SafeLoader)
    assert isinstance(root, MappingNode)
    for key, _node in root.value:
        if part:
            continue
        name = safe_load(text[key.start_mark.index : key.end_mark.index])
        for op in ("delete", "rename"):
            edit = Edit("", op, (name,), key="renamed_property")
            expected = deepcopy(original)
            value = expected.pop(name)
            if op == "rename":
                expected[edit.key] = value
            result, reformatted = apply_edits(text, [edit])
            assert safe_load(result) == expected
            if not reformatted:
                if op == "rename":
                    start, end = key.start_mark.index, key.end_mark.index
                    assert result == text[:start] + "renamed_property" + text[end:]
                else:
                    start = text.rfind("\n", 0, key.start_mark.index) + 1
                    # Deletion is one contiguous span starting at the key's line.
                    assert result[:start] == text[:start]
                    tail = result[start:]
                    assert text.endswith(tail)
    for index, (path, node) in enumerate(_leaves(root, text)):
        if index % parts != part:
            continue
        expected = deepcopy(original)
        parent = expected
        for part in path[:-1]:
            parent = parent[part]
        parent[path[-1]] = "replacement_leaf"
        result, reformatted = apply_edits(
            text, [Edit("", "replace", path, value="replacement_leaf")]
        )
        assert safe_load(result) == expected
        if not reformatted:
            assert (
                result
                == text[: node.start_mark.index] + "replacement_leaf" + text[node.end_mark.index :]
            )
