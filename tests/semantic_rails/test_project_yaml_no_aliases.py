import yaml

from semantic_rails.architect_scaffold import dump_project_yaml


def test_dump_project_yaml_omits_anchors_and_aliases():
    shared = [2026, 1, 1]
    document = {"from": shared, "to": shared}
    rendered = dump_project_yaml(document)
    assert "&id" not in rendered
    assert "*id" not in rendered
    assert yaml.safe_load(rendered) == document


def test_dump_project_yaml_keeps_the_aliases_a_self_referencing_document_needs():
    document: dict = {"label": "m"}
    document["spec"] = document
    loaded = yaml.safe_load(dump_project_yaml(document))
    assert loaded["spec"] is loaded
