"""Atomic route authoring through the project service and Architect MCP."""

from __future__ import annotations

import asyncio

import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

import semantic_rails.architect_service as service
from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.route_census import census_pairs, resolve_pairs, route_census, route_changes
from semantic_rails.runtime import Runtime
from tests.semantic_rails.test_route_census import (
    BASE_GOLD,
    INVOICE_BRANCH,
    INVOICE_ID,
    _answers,
    _files_and_receipts,
    _impact,
)
from tests.semantic_rails.test_route_clarification import (
    ACCOUNT,
    BALANCE_BY_DISTRICT,
    BRANCH_BY_KEY,
    BRANCH_ROUTE,
    BY_OWNER,
    DIAMOND_ROW,
    DISTRICT,
    _graph_rows,
    _project,
    _write_package,
)
from tests.semantic_rails.test_route_clarification import _gold as _district_gold
from tests.semantic_rails.test_route_resolution import (
    INVOICE,
    MEMBERSHIP,
    OWNER,
    OWNER_NAME,
    REGION,
    _gold,
    _pin,
    _query,
    _rows,
)
from tests.semantic_rails.test_route_resolution import _write_package as _write_bank


@pytest.fixture(autouse=True)
def _allow_external_package_paths(monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def _thirteen_routes(root):
    # A tree with an already-authored invoice -> region definition. Adding the
    # membership's owner key closes a cycle and moves thirteen unrecorded pairs.
    pkg = _write_bank(
        root,
        relationships=(
            "invoices_account",
            "accounts_branch_region",
            "owners_home_region",
            "memberships_account",
        ),
        pins=[_pin(INVOICE, REGION, INVOICE_BRANCH)],
    )
    seed = pkg / "data/seed.sql"
    seed.write_text(
        seed.read_text() + "\nALTER TABLE memberships ADD COLUMN owner_id INTEGER;\n"
        "UPDATE memberships SET owner_id = 10;\n"
    )
    return pkg


def _mcp_call(root, package, name, **arguments):
    async def call():
        server = create_architect_mcp_server(workspace_root=root)
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool(name, {"project_path": str(package), **arguments})
            assert not result.isError, result
            return dict(result.structuredContent or {})

    return asyncio.run(call())


def test_one_relationship_call_keeps_thirteen_routes_and_reference_answers(tmp_path):
    base = _thirteen_routes(tmp_path / "base")
    pkg = _thirteen_routes(tmp_path / "head")
    project = ArchitectProject(pkg, workspace_root=tmp_path)
    revision, files = project.revision(), _files_and_receipts(project)
    config = load_package_config(str(pkg))
    pairs = census_pairs(config)
    outcomes = {pair: outcome.shape() for pair, outcome in resolve_pairs(config, pairs).items()}
    assert _answers(pkg) == {name: _gold(sql) for name, sql in BASE_GOLD.items()}
    args = {"from_entity": "membership", "to_entity": "owner", "columns": ["owner_id"]}
    with pytest.raises(SemanticLayerError, match="keep_existing_routes=true") as refused:
        project.upsert_relationship(**args)
    assert refused.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert len(refused.value.details["route_changes"]) == 13
    assert _files_and_receipts(project) == files

    preview = project.upsert_relationship(**args, keep_existing_routes=True, dry_run=True).report
    assert preview["ok"] and preview["status"] == "preview"
    moved_pairs = {
        (row["source_entity"], row["target_entity"])
        for row in refused.value.details["route_changes"]
    }
    assert moved_pairs <= {
        (row["source_entity"], row["target_entity"]) for row in preview["kept_route_decisions"]
    }
    assert _files_and_receipts(project) == files
    committed = _mcp_call(
        tmp_path,
        pkg,
        "upsert_relationship",
        **args,
        keep_existing_routes=True,
        expected_revision=revision,
        idempotency_key="relate-and-keep",
    )
    assert committed["ok"] and committed["status"] == "upserted", committed
    assert committed["kept_route_decisions"] == preview["kept_route_decisions"]
    assert committed["revision"] == preview["proposed_revision"]
    assert committed["route_changes"] == []
    changed = load_package_config(str(pkg))
    assert {
        pair: outcome.shape() for pair, outcome in resolve_pairs(changed, pairs).items()
    } == outcomes
    assert (
        route_census(changed)
        == route_census(config)
        == {"undecided": [], "assumed": [], "pass_through": []}
    )
    assert _impact(base, pkg)["route_changes"] == []
    assert _answers(pkg) == {name: _gold(sql) for name, sql in BASE_GOLD.items()}
    assert (
        project.upsert_relationship(
            **args,
            keep_existing_routes=True,
            expected_revision=revision,
            idempotency_key="relate-and-keep",
        ).report["status"]
        == "replayed"
    )
    with pytest.raises(SemanticLayerError) as reused:
        project.upsert_relationship(
            **args, expected_revision=revision, idempotency_key="relate-and-keep"
        )
    assert reused.value.details["conflict_kind"] == "idempotency_key_reuse"


def _refused_own_key_package(root):
    pkg = _write_bank(
        root,
        relationships=(
            "invoices_account",
            "accounts_branch_region",
            "accounts_owner",
            "owners_home_region",
            "memberships_account",
        ),
        pins=[_pin(INVOICE, REGION, INVOICE_BRANCH)],
    )
    # Keep the existing graph bidirectional; the new owner key is forward-only.
    # This leaves the reverse ambiguous pair requiring an explicit author decision.
    package = yaml.safe_load((pkg / "package.yml").read_text())
    package["defaults"]["relationship"]["traversal"] = ["forward"]
    (pkg / "package.yml").write_text(yaml.safe_dump(package))
    graph = yaml.safe_load((pkg / "graph.yml").read_text())
    for edge in graph["graph"]["relationships"].values():
        edge["allowed_directions"] = ["forward", "reverse"]
    graph["graph"]["relationships"]["invoices_account"]["rollup_safe"] = {
        "reverse": ["count_distinct"]
    }
    # A second invoice role provides another previously answered pair to preserve.
    graph["graph"]["entities"]["receipt"] = {"key": ["invoice_id"], "model": "receipts"}
    (pkg / "models/receipts.yml").write_text(
        yaml.safe_dump(
            {"model": {"id": "receipts", "relation": "invoices", "entities": {"receipt": {}}}}
        )
    )
    graph["graph"]["relationships"]["invoices_receipt"] = {
        "id": "relationship.invoices_receipt",
        "entities": ["invoice", "receipt"],
        "cardinality": "one_to_one",
        "via": ["invoice_id"],
        "target": ["invoice_id"],
        "allowed_directions": ["forward", "reverse"],
    }
    (pkg / "graph.yml").write_text(yaml.safe_dump(graph))
    membership = pkg / "models/memberships.yml"
    model = yaml.safe_load(membership.read_text())
    model["model"]["measures"] = {
        "membership_count": {
            "kind": "entity_count",
            "entity_key": "membership_id",
            "accumulation": {"kind": "population"},
            "value_type": "count",
        }
    }
    membership.write_text(yaml.safe_dump(model))
    seed = pkg / "data/seed.sql"
    seed.write_text(
        seed.read_text() + "\nALTER TABLE memberships ADD COLUMN owner_id INTEGER;\n"
        "UPDATE memberships SET owner_id = 10;\n"
    )
    return pkg


def test_keep_choice_allows_an_answer_opened_by_the_relationship_only(tmp_path):
    base = _refused_own_key_package(tmp_path / "base")
    pkg = _refused_own_key_package(tmp_path / "head")
    relationship_only = _refused_own_key_package(tmp_path / "relationship-only")
    # Author the same foreign key without any generated route decisions.
    model_path = relationship_only / "models/memberships.yml"
    model = yaml.safe_load(model_path.read_text())
    model["model"]["entities"]["owner"] = {"expr": "owner_id"}
    model_path.write_text(yaml.safe_dump(model))
    before = load_package_config(str(base))
    changed = load_package_config(str(relationship_only))
    pairs = census_pairs(before)
    outcomes = resolve_pairs(before, pairs)
    opened = (MEMBERSHIP, OWNER)
    assert outcomes[opened].shape() == {"refused": "AMBIGUOUS_PATH"}
    own_key = resolve_pairs(changed, [opened])[opened]
    assert own_key.basis == "colocated_key"
    assert own_key.shape() == {"relationship_path": ["relationship.memberships_owner"]}
    moved = [row for row in route_changes(before, changed) if "relationship_path" in row["base"]]
    assert len(moved) >= 2

    project = ArchitectProject(pkg, workspace_root=tmp_path)
    report = project.upsert_relationship(
        from_entity="membership", to_entity="owner", columns=["owner_id"], keep_existing_routes=True
    ).report
    assert report["ok"] and report["status"] == "upserted"
    final = load_package_config(str(pkg))
    final_outcomes = resolve_pairs(final, pairs)
    assert {
        pair: outcome.shape()
        for pair, outcome in final_outcomes.items()
        if not outcomes[pair].refused
    } == {pair: outcome.shape() for pair, outcome in outcomes.items() if not outcome.refused}
    assert final_outcomes[opened].shape() == own_key.shape()
    expected_changes = [
        {
            "source_entity": MEMBERSHIP,
            "target_entity": OWNER,
            "base": outcomes[opened].shape(),
            "head": own_key.shape(),
        }
    ]
    assert report["route_changes"] == _impact(base, pkg)["route_changes"] == expected_changes
    # Only the relationship's own answer leaves the undecided census; answered pairs stay out.
    before_census = route_census(before)
    final_census = route_census(final)
    assert [(row["source_entity"], row["target_entity"]) for row in final_census["undecided"]] == [
        (row["source_entity"], row["target_entity"])
        for row in before_census["undecided"]
        if (row["source_entity"], row["target_entity"]) != opened
    ]
    assert final_census["assumed"] == before_census["assumed"]
    runtime = Runtime.from_path(str(pkg))
    try:
        preserved = runtime.query(
            _query(
                "measure.bank.membership_count",
                where=[{"field": INVOICE_ID, "op": "=", "value": 1000}],
            )
        )
        newly_answered = runtime.query(
            _query("measure.bank.membership_count", group_by=[OWNER_NAME])
        )
    finally:
        runtime.close()
    assert _rows(preserved, ["v"]) == _gold(
        "SELECT COUNT(DISTINCT m.membership_id) FROM memberships m "
        "JOIN invoices i ON i.account_id = m.account_id WHERE i.invoice_id = 1000"
    )
    assert _rows(newly_answered, [OWNER_NAME, "v"]) == _gold(
        "SELECT o.owner_name, COUNT(DISTINCT m.membership_id) FROM memberships m "
        "JOIN owners o ON o.owner_id = 10 GROUP BY 1"
    )


def test_keep_choice_refuses_when_preservation_is_bypassed(tmp_path, monkeypatch):
    project = ArchitectProject(_thirteen_routes(tmp_path), workspace_root=tmp_path)
    files = _files_and_receipts(project)
    monkeypatch.setattr(
        project,
        "_prepare_route_decisions",
        lambda transaction, config, rows, updates: (updates, []),
    )
    with pytest.raises(SemanticLayerError) as refused:
        project.upsert_relationship(
            from_entity="membership",
            to_entity="owner",
            columns=["owner_id"],
            keep_existing_routes=True,
            validate_after=False,
        )
    assert refused.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert len(refused.value.details["route_changes"]) == 13
    assert _files_and_receipts(project) == files


def test_keep_choice_cannot_open_an_inherited_answer_with_generated_pins(tmp_path):
    pkg = _refused_own_key_package(tmp_path)
    package = yaml.safe_load((pkg / "package.yml").read_text())
    package["defaults"]["relationship"]["traversal"] = ["forward", "reverse"]
    (pkg / "package.yml").write_text(yaml.safe_dump(package))
    project = ArchitectProject(pkg, workspace_root=tmp_path)
    files = _files_and_receipts(project)
    with pytest.raises(SemanticLayerError) as refused:
        project.upsert_relationship(
            from_entity="membership",
            to_entity="owner",
            columns=["owner_id"],
            keep_existing_routes=True,
        )
    assert refused.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert refused.value.details["route_changes"] == [
        {
            "source_entity": OWNER,
            "target_entity": MEMBERSHIP,
            "base": {"refused": "AMBIGUOUS_PATH"},
            "head": {"relationship_path": ["relationship.memberships_owner"]},
        }
    ]
    assert _files_and_receipts(project) == files


def test_bulk_replaces_conflicting_forward_and_reverse_rows_together(tmp_path):
    reverse = _pin(DISTRICT, ACCOUNT, BRANCH_ROUTE[::-1])
    project = ArchitectProject(
        _write_package(tmp_path, decisions=[BRANCH_BY_KEY, reverse]), workspace_root=tmp_path
    )
    rows = [DIAMOND_ROW, _pin(DISTRICT, ACCOUNT, DIAMOND_ROW["relationship_path"][::-1])]
    files, revision = _files_and_receipts(project), project.revision()
    with pytest.raises(SemanticLayerError):
        project.record_route_decision(**DIAMOND_ROW)
    preview = project.record_route_decision(decisions=rows, dry_run=True).report
    assert preview["ok"] and preview["status"] == "preview"
    assert _files_and_receipts(project) == files
    committed = _mcp_call(
        tmp_path,
        project.project_path,
        "record_route_decision",
        decisions=rows,
        expected_revision=revision,
        idempotency_key="both-directions",
    )
    assert committed["ok"] and committed["status"] == "recorded", committed
    assert committed["revision"] == preview["proposed_revision"]
    assert [report["replaced"] for report in committed["route_decisions"]] == [
        BRANCH_BY_KEY,
        reverse,
    ]
    assert _graph_rows(project) == rows
    runtime = Runtime.from_path(str(project.project_path))
    try:
        out = runtime.query(BALANCE_BY_DISTRICT)
    finally:
        runtime.close()
    assert _rows(out, ["dimension.bank_district_name", "v"]) == _district_gold(BY_OWNER)
    assert (
        project.record_route_decision(
            decisions=rows, expected_revision=revision, idempotency_key="both-directions"
        ).report["status"]
        == "replayed"
    )
    with pytest.raises(SemanticLayerError) as stale:
        project.record_route_decision(
            decisions=rows, expected_revision=revision, idempotency_key="new-at-stale-revision"
        )
    assert stale.value.code == "CONFIG_CONFLICT"


@pytest.mark.parametrize(
    "bad_row",
    [
        {**DIAMOND_ROW, "relationship_path": ["relationship.unknown"]},
        {**DIAMOND_ROW, "target_entity": "unknown"},
        {**DIAMOND_ROW, "relationship_path": BRANCH_ROUTE},  # duplicate pair
        _pin(DISTRICT, ACCOUNT, BRANCH_ROUTE[::-1]),  # contradicts first row
        {**DIAMOND_ROW, "relationship_path": "accounts_owner"},
        {**DIAMOND_ROW, "typo": "ignored?"},
        None,
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_one_bad_bulk_row_refuses_all_files_and_receipts(tmp_path, bad_row, dry_run):
    project = _project(tmp_path)
    files = _files_and_receipts(project)
    with pytest.raises(SemanticLayerError) as refused:
        project.record_route_decision(decisions=[DIAMOND_ROW, bad_row], dry_run=dry_run)
    assert refused.value.code == "INVALID_CONFIG"
    assert _files_and_receipts(project) == files


@pytest.mark.parametrize(
    "arguments",
    [
        {"decisions": []},
        {"decisions": [DIAMOND_ROW], **DIAMOND_ROW},
        {"decisions": [DIAMOND_ROW], "label": "mixed"},
        {},
    ],
)
def test_bulk_input_requires_exactly_one_nonempty_form(tmp_path, arguments):
    project = _project(tmp_path)
    files = _files_and_receipts(project)
    with pytest.raises(SemanticLayerError) as refused:
        project.record_route_decision(**arguments)
    assert refused.value.code == "INVALID_CONFIG"
    assert _files_and_receipts(project) == files


def test_bulk_effectiveness_guard_checks_every_row(tmp_path, monkeypatch):
    project = _project(tmp_path)
    files = _files_and_receipts(project)
    resolve = service.package_route
    reverse = _pin(DISTRICT, ACCOUNT, DIAMOND_ROW["relationship_path"][::-1])

    def elsewhere(config, *, start, target):
        resolution = resolve(config, start=start, target=target)
        if start == DISTRICT:
            return resolution._replace(routes=(tuple(BRANCH_ROUTE[::-1]),))
        return resolution

    monkeypatch.setattr(service, "package_route", elsewhere)
    with pytest.raises(SemanticLayerError) as refused:
        project.record_route_decision(decisions=[DIAMOND_ROW, reverse])
    assert refused.value.details["reason"] == "route_decision_not_in_effect"
    assert refused.value.details["route_decision"] == reverse
    assert _files_and_receipts(project) == files
