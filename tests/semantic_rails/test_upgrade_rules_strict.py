"""Keys the loader never read are deleted when it reads the same value elsewhere, else asked."""

import pytest
import yaml

from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.upgrade.model import PackageFiles, plan
from semantic_rails.upgrade.rules_strict import RULES as STRICT_RULES
from semantic_rails.yaml_loader import safe_load
from tests.semantic_rails.conftest import write_single_file_package

RULES = tuple(rule for rule in STRICT_RULES if rule.id == "ignored-key")


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


def _current(doc):
    """The starter as current authoring writes it: graph-bound models and no grain."""
    for entity, model in (
        ("customer", "customers"),
        ("order", "orders"),
        ("order_item", "order_items"),
        ("product", "products"),
    ):
        doc["models"][model].pop("grain", None)
        doc["graph"]["entities"][entity]["model"] = model


def _model(name, **keys):
    def edit(doc):
        doc["models"][name].update(keys)

    return edit


def _unbind(entity, model, **keys):
    def edit(doc):
        del doc["graph"]["entities"][entity]["model"]
        doc["models"][model].update(keys)

    return edit


def _entity(name, **keys):
    def edit(doc):
        doc["graph"]["entities"][name].update(keys)

    return edit


def _row(model, block, key, **keys):
    def edit(doc):
        doc["models"][model][block][key].update(keys)

    return edit


def _singular(**keys):
    def edit(doc):
        del doc["models"]["products"]["entities"]
        doc["models"]["products"].update(entity="product", **keys)

    return edit


def _relationship(doc):
    doc["graph"]["relationships"] = {
        "orders_customer": {
            "entities": ["order", "customer"],
            "cardinality": "many_to_one",
            "label": "Buyer",
        }
    }


def _unpublished(publish):
    """A package of the default profile whose revenue measure alone publishes a metric."""

    def edit(doc):
        doc["package"].pop("schema_strict", None)
        doc["metrics"] = {}
        doc["models"]["orders"]["measures"]["order_count"]["publish"] = False
        doc["models"]["order_items"]["measures"]["line_revenue_usd"]["publish"] = False
        doc["models"]["orders"]["measures"]["revenue_usd"]["publish"] = publish

    return edit


def _authored(doc):
    _unpublished(False)(doc)
    doc["metrics"] = {
        "revenue_usd": {
            "kind": "aggregate",
            "measure": "measure.shop.revenue_usd",
            "aggregation": "sum",
            "label": "Gross revenue",
            "description": "Gross order revenue.",
            "value_type": "currency",
            "temporal_role": "temporal_role.shop_order_ordered_at",
            "meta": {
                "owner_team": "finance_analytics",
                "review_priority": "high",
                "change_risk": "medium",
            },
        }
    }


def _strict(edit):
    def wrapped(doc):
        edit(doc)
        doc["package"]["schema_strict"] = True

    return wrapped


GROSS = {"label": "Gross revenue", "description": "Gross order revenue."}
# rule, legacy edit, current edit (None: the rule stops and names the current form)
REWRITES = {
    "grain-equals-bound-key": ("model-grain", _model("orders", grain=["order_id"]), _current),
    "grain-picks-unbound-entity": (
        "model-grain",
        _unbind("order", "orders", grain=["order_id"]),
        _current,
    ),
    "grain-finer-than-entity": (
        "model-grain",
        _model("orders", grain=["order_id", "channel"]),
        None,
    ),
    "keys-primary-beside-entities": (
        "model-primary-key",
        _model("orders", keys={"primary": ["order_id"]}),
        _current,
    ),
    "keys-foreign-beside-entities": (
        "model-primary-key",
        _model("orders", keys={"foreign": {"customer": "customer_id"}}),
        _current,
    ),
    "singular-entity-keys": (
        "model-primary-key",
        _singular(keys={"primary": ["product_id"]}),
        _model("products", entity="product"),
    ),
    "singular-entity-grain": (
        "model-primary-key",
        _singular(grain=["product_id"]),
        _model("products", entity="product"),
    ),
    "singular-entity-no-row-key": ("model-primary-key", _singular(), None),
    "keys-primary-finer": (
        "model-primary-key",
        _model("orders", keys={"primary": ["order_id", "channel"]}),
        None,
    ),
    "join-to-relationship": (
        "model-joins",
        _model(
            "orders",
            joins={"customer": {"to": "customer", "cardinality": "N:1", "label": "Buyer"}},
        ),
        _relationship,
    ),
    "join-key-names-no-column": (
        "model-joins",
        _model("orders", joins={"buyer": {"to": "customer"}}),
        None,
    ),
    "entity-id-derived": ("object-as", _entity("order", id="entity.shop_order"), _current),
    "entity-id-public": (
        "object-as",
        _entity("order", id="entity.public_order"),
        _entity("order", **{"as": "entity.public_order"}),
    ),
    "dimension-id-derived": (
        "object-as",
        _row("orders", "dimensions", "channel", id="dimension.shop_order_channel"),
        _current,
    ),
    "measure-id-derived": (
        "object-as",
        _row("orders", "measures", "revenue_usd", id="measure.shop.revenue_usd"),
        _current,
    ),
    "dimension-id-public": (
        "object-as",
        _row("orders", "dimensions", "channel", id="dimension.public_channel"),
        _row("orders", "dimensions", "channel", **{"as": "dimension.public_channel"}),
    ),
    "id-beside-as": (
        "object-as",
        _row(
            "orders",
            "measures",
            "order_count",
            id="measure.x",
            **{"as": "measure.shop.order_count"},
        ),
        _row("orders", "measures", "order_count", **{"as": "measure.shop.order_count"}),
    ),
    "join-cardinality-never-read": (
        "model-joins",
        _model("orders", joins={"customer": {"to": "customer", "cardinality": "many_to_one"}}),
        None,
    ),
    "publish-ignored-by-strict": (
        "measure-auto-publish",
        _strict(_row("orders", "measures", "revenue_usd", publish=GROSS)),
        _strict(_current),
    ),
    "publish-authors-metric": ("measure-auto-publish", _unpublished(GROSS), _authored),
    "publish-topics": (
        "measure-auto-publish",
        _unpublished({**GROSS, "topics": ["sales"]}),
        None,
    ),
}


@pytest.mark.parametrize(("rule", "legacy", "current"), REWRITES.values(), ids=REWRITES)
def test_strict_rule_golden_rewrite(tmp_path, rule, legacy, current):
    rules = [row for row in STRICT_RULES if row.id == rule]

    def write(root, edit):
        source = write_single_file_package(root / "project")
        doc = safe_load(source.read_bytes())
        _current(doc)
        edit(doc)
        source.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        return source

    source = write(tmp_path / "legacy", legacy)
    files = PackageFiles(source)
    result = plan(files, rules, {})
    if current is None:
        (stop,) = result.pending
        assert not stop.edits and not stop.options and not result.files
        return
    assert not result.pending and result.findings
    expected = write(tmp_path / "current", current)
    assert safe_load(result.files[source.name]) == safe_load(expected.read_bytes())
    upgraded = PackageFiles(source, contents={**files.contents, **result.files})
    assert not plan(upgraded, rules, {}).findings
