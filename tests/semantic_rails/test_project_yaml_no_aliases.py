import yaml

from semantic_rails.architect_scaffold import dump_project_yaml


def test_dump_project_yaml_omits_anchors_and_aliases():
    shared = [2026, 1, 1]
    document = {"from": shared, "to": shared}
    rendered = dump_project_yaml(document)
    assert "&" not in rendered
    assert "*id" not in rendered
    assert yaml.safe_load(rendered) == document
