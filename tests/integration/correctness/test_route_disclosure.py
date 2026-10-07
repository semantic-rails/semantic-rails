"""What an answer discloses about the route it read, on DuckDB and Postgres.

Fixture ``teams/``: members hold no team key, so "members by team plan tier" reads the Team
of any of the Member's events, through Member event rows. A member with no event (Di) drops
out, and one with events in two teams (Ann) counts under both: the answer says so with
ROUTE_PASS_THROUGH, at every verbosity, and how to declare what the question means. Team
signups read a Plan through the billing version valid at the signup's time: the January
teams' first versions start 5 ms after their signups, so every January signup reads an
empty Plan, and the answer says how much of it does (NULL_PRESERVING_HISTORY).

Disclosure only: every number equals independent reference SQL and the route the engine
reads is unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import route_reading
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.runtime import Runtime
from tests.semantic_rails.hidden_absent import visibility_policy, with_policies

from .conftest import _postgres, _rows, _runtime, _strict

TEAMS = Path(__file__).resolve().parent / "teams"
SEED = (TEAMS / "data" / "seed.sql").read_text(encoding="utf-8")

MEMBER, MEMBER_EVENT, TEAM = "entity.teams_member", "entity.teams_member_event", "entity.teams_team"
SIGNUP, PLAN = "entity.teams_team_signup", "entity.teams_plan"
TIER = "dimension.teams_team_plan_tier"
PLAN_NAME = "dimension.teams_plan_plan_name"
EVENT_KIND = "dimension.teams_member_event_event_kind"
THROUGH_EVENTS = ["relationship.member_events_member", "relationship.member_events_team"]
TO_PLAN = ["relationship.billing_version_team", "relationship.team_billing_history_plan"]
SIGNED_UP = "temporal_role.teams_team_signup_signed_up_at"
CREATED = "temporal_role.teams_team_created_at"
JANUARY = {"start": "2024-01-01", "end": "2024-02-01"}
MARCH = {"start": "2024-03-01", "end": "2024-04-01"}


def _select(measure: str, alias: str, **query: Any) -> dict[str, Any]:
    return {
        "select": [{"expression": {"measure": f"measure.teams.{measure}"}, "as": alias}]
    } | query


MEMBERS_BY_TIER = _select("member_count", "members", group_by=[TIER])
PRO_MEMBERS = _select("member_count", "members", where=[{"field": TIER, "op": "=", "value": "pro"}])
PRO_MEMBERS_GROUP = _select(
    "member_count",
    "members",
    where=[
        {
            "child": MEMBER_EVENT,
            "match": "any",
            "where": [{"field": TIER, "op": "=", "value": "pro"}],
        }
    ],
)
VALID_PLAN = """
  LEFT JOIN team_billing_history h ON h.team_id = {team} AND h.valid_from <= {at}
    AND (h.valid_to > {at} OR h.valid_to IS NULL)
  LEFT JOIN plans p ON p.plan_id = h.plan_id"""
REFERENCE = {
    "members_by_tier": """SELECT t.plan_tier, COUNT(DISTINCT e.member_id) FROM member_events e
      JOIN teams t ON t.team_id = e.team_id GROUP BY t.plan_tier""",
    "pro_members": """SELECT COUNT(*) FROM members m WHERE EXISTS (SELECT 1 FROM member_events e
      JOIN teams t ON t.team_id = e.team_id WHERE e.member_id = m.member_id
      AND t.plan_tier = 'pro')""",
    "new_teams_by_plan": "SELECT p.plan_name, COUNT(*) FROM team_signups s"
    + VALID_PLAN.format(team="s.team_id", at="s.signed_up_at")
    + " WHERE s.signed_up_at >= TIMESTAMP '{start}' AND s.signed_up_at < TIMESTAMP '{end}'"
    " GROUP BY p.plan_name",
    "teams_by_plan": "SELECT p.plan_name, COUNT(*) FROM teams t"
    + VALID_PLAN.format(team="t.team_id", at="t.created_at")
    + " WHERE t.created_at >= TIMESTAMP '2024-03-01' AND t.created_at < TIMESTAMP '2024-04-01'"
    " GROUP BY p.plan_name",
    "plans_by_tier": """SELECT t.plan_tier, COUNT(DISTINCT h.plan_id) FROM team_billing_history h
      JOIN teams t ON t.team_id = h.team_id GROUP BY t.plan_tier""",
    "seats_by_tier": """SELECT t.plan_tier, SUM(h.seats) FROM team_billing_history h
      JOIN teams t ON t.team_id = h.team_id GROUP BY t.plan_tier""",
    "events_by_tier": """SELECT t.plan_tier, COUNT(*) FROM member_events e
      JOIN teams t ON t.team_id = e.team_id GROUP BY t.plan_tier""",
    "teams_with_posts": """SELECT COUNT(*) FROM teams t WHERE EXISTS (SELECT 1 FROM
      member_events e WHERE e.team_id = t.team_id AND e.event_kind = 'post')""",
    "signups_with_posts": """SELECT COUNT(*) FROM team_signups s WHERE EXISTS (SELECT 1 FROM
      member_events e WHERE e.team_id = s.team_id AND e.event_kind = 'post')""",
}


def _edit(package: Path, name: str, change: Callable[[dict[str, Any]], None]) -> None:
    path = package / name
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(spec)
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")


def _bridge(spec: dict[str, Any]) -> None:
    spec["model"]["entities"]["bridge"] = True


def _recorded(spec: dict[str, Any]) -> None:
    # A Member's Plan: the Plan of any of the Member's events' Team's billing version.
    spec["graph"]["path_preferences"] = [
        {
            "source_entity": "member",
            "target_entity": "plan",
            "relationship_path": [*THROUGH_EVENTS, *TO_PLAN],
        }
    ]


def _home_team(spec: dict[str, Any]) -> None:
    spec["model"]["dimensions"]["team_id"] = {"label": "Home team", "kind": "categorical"}


VARIANTS: dict[str, tuple[str, Callable[[dict[str, Any]], None]] | None] = {
    "base": None,
    "bridge": ("models/member_events.yml", _bridge),
    "recorded": ("graph.yml", _recorded),
    "home_team": ("models/members.yml", _home_team),
}


@pytest.fixture(scope="module")
def packages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    made = {}
    for name, edit in VARIANTS.items():
        package = tmp_path_factory.mktemp(f"teams_{name}") / "teams"
        shutil.copytree(TEAMS, package)
        if edit is not None:
            _edit(package, *edit)
        made[name] = package
    return made


class Teams:
    def __init__(self, runtimes: dict[str, Runtime]) -> None:
        self.runtimes = runtimes

    def reference(self, name: str, **values: str) -> list[tuple[Any, ...]]:
        return sorted(_rows(self.runtimes["base"], REFERENCE[name].format(**values)), key=repr)

    def answer(self, query: dict[str, Any], variant: str = "base") -> list[tuple[Any, ...]]:
        return _values(self.runtimes[variant].query(query))


def _values(out: dict[str, Any]) -> list[tuple[Any, ...]]:
    return sorted((tuple(row.values()) for row in out["rows"]), key=repr)


@pytest.fixture(scope="module", params=["duckdb", "postgres"])
def teams(request: pytest.FixtureRequest, packages: dict[str, Path]) -> Iterator[Teams]:
    if request.param == "duckdb":
        runtimes = {name: _runtime(path) for name, path in packages.items()}
        try:
            for runtime in runtimes.values():
                _rows(runtime, "SELECT 1")  # the fixture owns each connection
            yield Teams(runtimes)
        finally:
            for runtime in runtimes.values():
                runtime.close()
        return
    required = ("SR_POSTGRES_HOST", "SR_POSTGRES_USER", "SR_POSTGRES_PASSWORD")
    if any(not os.environ.get(name, "").strip() for name in required):
        pytest.skip("postgres: SR_POSTGRES_* unset (run through with_postgres.sh)")
    schema = f"sr_teams_{uuid.uuid4().hex[:12]}"
    options = {
        "host_env": "SR_POSTGRES_HOST",
        "port": os.environ.get("SR_POSTGRES_PORT", "5433"),
        "database": os.environ.get("SR_POSTGRES_DATABASE", "sr_jaffle"),
        "user_env": "SR_POSTGRES_USER",
        "password_env": "SR_POSTGRES_PASSWORD",
    }
    admin = _postgres(packages["base"], options)
    try:
        _rows(admin, f"CREATE SCHEMA {schema}")
    except Exception as exc:
        admin.close()
        if _strict():
            raise
        pytest.skip(f"postgres: unreachable ({type(exc).__name__})")
    runtimes: dict[str, Runtime] = {}
    try:
        for name, path in packages.items():
            runtimes[name] = _postgres(path, {**options, "schema": schema})
        _rows(runtimes["base"], SEED)
        yield Teams(runtimes)
    finally:
        for runtime in runtimes.values():
            runtime.close()
        try:
            _rows(admin, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()


def _codes(out: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [warning for warning in out["warnings"] if warning["code"] == code]


def _pass_through(out: dict[str, Any]) -> dict[str, Any]:
    (note,) = _codes(out, "ROUTE_PASS_THROUGH")
    return note


@pytest.mark.parametrize(
    ("query", "reference"),
    [(MEMBERS_BY_TIER, "members_by_tier"), (PRO_MEMBERS, "pro_members")],
    ids=["grouped", "filtered"],
)
@pytest.mark.parametrize("verbosity", ["minimal", "full"])
def test_a_route_through_another_tables_rows_says_what_it_counts(
    teams, query, reference, verbosity
):
    out = teams.runtimes["base"].query({**query, "verbosity": verbosity})
    assert _values(out) == teams.reference(reference)  # pro 3 (Ann, Bo, Cy), free 2 (Ann, Ed)
    note = _pass_through(out)
    meaning = "the Team of any of the Member's Member events"
    assert note["severity"] == "warning"
    assert note["object_ids"] == [MEMBER, TEAM]
    assert note["message"] == (
        "The Team of any of the Member's Member events, through Member event rows the package "
        "doesn't declare as a link table: a Member with no Member event is left out, and one "
        "with several can count under several Teams. Fix: declare the Member's own Team key, "
        "declare Member event a link table (`bridge: true`), record this route "
        "(graph.path_preferences), or ask about the Member events with a child group."
    )
    assert len(json.dumps(note)) < 1500
    where = query.get("where", [])
    assert note["details"] == {
        "route": THROUGH_EVENTS,
        "meaning": meaning,
        "through": [
            {
                "entity": MEMBER_EVENT,
                "enters_by": THROUGH_EVENTS[0],
                "leaves_by": THROUGH_EVENTS[1],
            }
        ],
        "fixes": {
            "declare_key": {"entity": MEMBER, "target_entity": TEAM, "columns": ["team_id"]},
            "declare_link_table": [{"entity": MEMBER_EVENT, "entities": {"bridge": True}}],
            "record_route": {
                "source_entity": MEMBER,
                "target_entity": TEAM,
                "relationship_path": THROUGH_EVENTS,
            },
            "child_group": {"child": MEMBER_EVENT, "match": "any", "where": where},
        },
    }


def test_the_pass_through_warning_shows_on_every_surface(teams):
    runtime = teams.runtimes["base"]
    for out in (runtime.validate(MEMBERS_BY_TIER), runtime.compile(MEMBERS_BY_TIER)):
        assert _pass_through(out)["object_ids"] == [MEMBER, TEAM]
    response = SemanticLayerMCPAdapter(runtime).call_tool(
        "execute", {"query": MEMBERS_BY_TIER, "mode": "run", "verbosity": "minimal"}
    )
    assert "ROUTE_PASS_THROUGH" in json.dumps(response)


def test_the_key_fix_names_a_column_the_start_already_reads(teams):
    out = teams.runtimes["home_team"].query(MEMBERS_BY_TIER)
    assert _values(out) == teams.reference("members_by_tier")
    assert (
        "Fix: declare the Member's own Team key (its table has `team_id`),"
        in (_pass_through(out)["message"])
    )


@pytest.mark.parametrize(
    ("variant", "query", "reference"),
    [
        ("bridge", MEMBERS_BY_TIER, "members_by_tier"),
        ("bridge", PRO_MEMBERS, "pro_members"),
        ("recorded", MEMBERS_BY_TIER, "members_by_tier"),
        ("recorded", PRO_MEMBERS, "pro_members"),
        ("base", PRO_MEMBERS_GROUP, "pro_members"),
        # Through a history (it holds the validity window), then on to its team.
        ("base", _select("plan_count", "plans", group_by=[TIER]), "plans_by_tier"),
        # Lookups only, a descent only, and a lookup then a descent.
        ("base", _select("event_count", "events", group_by=[TIER]), "events_by_tier"),
        (
            "base",
            _select(
                "team_count", "teams", where=[{"field": EVENT_KIND, "op": "=", "value": "post"}]
            ),
            "teams_with_posts",
        ),
        (
            "base",
            _select("new_teams", "new", where=[{"field": EVENT_KIND, "op": "=", "value": "post"}]),
            "signups_with_posts",
        ),
    ],
    ids=[
        "link_table_grouped",
        "link_table_filtered",
        "recorded_row_grouped",
        "recorded_row_filtered",
        "child_group",
        "through_history",
        "up",
        "down",
        "up_then_down",
    ],
)
def test_a_declared_crossing_or_no_crossing_answers_without_the_warning(
    teams, variant, query, reference
):
    out = teams.runtimes[variant].query({**query, "verbosity": "full"})
    assert _values(out) == teams.reference(reference)
    assert not _codes(out, "ROUTE_PASS_THROUGH")


def test_an_authored_bridge_is_kept_on_the_entity_and_in_the_fingerprint(packages):
    base, bridged = (load_package_snapshot(packages[name]) for name in ("base", "bridge"))
    flags = {
        name: {row.id for row in snapshot.config.entities if row.bridge}
        for name, snapshot in (("base", base), ("bridge", bridged))
    }
    assert flags == {"base": set(), "bridge": {MEMBER_EVENT}}
    assert base.semantic_fingerprint != bridged.semantic_fingerprint


def test_a_query_that_decides_the_route_gets_only_its_own_note(teams):
    row = {"source_entity": MEMBER, "target_entity": TEAM, "relationship_path": THROUGH_EVENTS}
    out = teams.runtimes["base"].query(
        {**MEMBERS_BY_TIER, "route_decisions": [row], "verbosity": "full"}
    )
    assert _values(out) == teams.reference("members_by_tier")
    assert not _codes(out, "ROUTE_PASS_THROUGH")
    (note,) = _codes(out, "ROUTE_CHOSEN_BY_QUERY")
    assert note["details"]["replaced"] == "only_route"


def test_a_caller_who_cannot_see_the_link_table_reads_nothing_naming_it(packages):
    config = load_package_config(str(packages["base"]))
    hidden = with_policies(config, visibility_policy("hidden", MEMBER_EVENT))
    runtime = Runtime.from_config(hidden, source_path=str(packages["base"]))
    try:
        with pytest.raises(SemanticLayerError) as refused:
            runtime.query(
                {**MEMBERS_BY_TIER, "verbosity": "full", "policy_context": {"roles": ["support"]}}
            )
    finally:
        runtime.close()
    text = json.dumps({"message": str(refused.value), "details": refused.value.details})
    assert "member_event" not in text.lower()
    assert "Member event" not in text


def _new_teams(window: dict[str, str], **query: Any) -> dict[str, Any]:
    return _select(
        "new_teams",
        "new_teams",
        group_by=[PLAN_NAME],
        time={"temporal_role": SIGNED_UP, **window},
        **query,
    )


def _history(out: dict[str, Any]) -> list[dict[str, Any]]:
    return _codes(out, "NULL_PRESERVING_HISTORY")


STATIC = "Plan: the Billing version valid at each row's time; rows with none read an empty Plan."


def test_an_empty_history_group_is_counted_from_the_returned_rows(teams):
    runtime = teams.runtimes["base"]
    query = _new_teams(JANUARY)
    out = runtime.query(query)
    assert _values(out) == teams.reference("new_teams_by_plan", **JANUARY) == [(None, 2)]
    (warning,) = _history(out)
    assert warning["message"] == (
        "2 of 2 new teams (100%) are in the empty Plan group: no Billing version was valid at "
        "their time, or it has no Plan."
    )
    assert warning["details"] == {
        "use": "grouping",
        "dimension": PLAN_NAME,
        "entity": "entity.teams_billing_version",
        "relationships": ["relationship.billing_version_team"],
        "null_rows": 1,
        "rows": 1,
        "measures": [{"output": "new_teams", "null_value": 2, "total": 2}],
    }
    for compiled in (runtime.validate(query), runtime.compile(query)):
        assert [row["message"] for row in _history(compiled)] == [STATIC]


def test_a_complete_answer_with_no_empty_group_drops_the_history_warning(teams):
    runtime = teams.runtimes["base"]
    query = _new_teams(MARCH)
    out = runtime.query(query)
    assert _values(out) == teams.reference("new_teams_by_plan", **MARCH)
    assert _values(out) == [("Builder", 1), ("Pro", 1)]
    assert not _history(out)
    for compiled in (runtime.validate(query), runtime.compile(query)):
        assert [row["message"] for row in _history(compiled)] == [STATIC]
    # A limited answer may have left the empty group out: the warning stays as compiled.
    limited = runtime.query({**query, "limit": 1, "order_by": [{"field": "new_teams"}]})
    assert limited["row_count"] == 1
    assert [row["message"] for row in _history(limited)] == [STATIC]
    # Team -> its billing version at the team's time -> Plan, rooted at the team.
    teams_by_plan = _select(
        "team_count", "teams", group_by=[PLAN_NAME], time={"temporal_role": CREATED, **MARCH}
    )
    by_team = runtime.query(teams_by_plan)
    assert _values(by_team) == teams.reference("teams_by_plan")
    assert not _history(by_team) and not _codes(by_team, "ROUTE_PASS_THROUGH")


def test_a_filter_through_a_history_says_it_leaves_rows_out(teams):
    runtime = teams.runtimes["base"]
    query = _select(
        "new_teams",
        "new_teams",
        where=[{"field": PLAN_NAME, "op": "=", "value": "Builder"}],
        time={"temporal_role": SIGNED_UP, **JANUARY},
    )
    out = runtime.query(query)
    assert [row["new_teams"] or 0 for row in out["rows"]] in ([], [0])
    expected = (
        "Plan filter: rows with no Billing version valid at their time are left out by this filter."
    )
    for response in (out, runtime.validate(query), runtime.compile(query)):
        assert [row["message"] for row in _history(response)] == [expected]
        assert _history(response)[0]["details"]["use"] == "filter"


def test_a_path_that_only_leaves_a_history_warns_of_nothing(teams):
    query = _select("billed_seats", "seats", group_by=[TIER])
    runtime = teams.runtimes["base"]
    out = runtime.query(query)
    assert _values(out) == teams.reference("seats_by_tier")
    assert not _history(out) and not _history(runtime.validate(query))


def test_a_hop_into_a_history_reads_valid_at_the_time(packages):
    config = load_package_config(str(packages["base"]))
    assert route_reading(config, SIGNUP, ["relationship.team_signups_team", *TO_PLAN]) == (
        "the Plan of the Billing version valid at the time of the Team signup's Team"
    )
    assert route_reading(config, MEMBER, [*THROUGH_EVENTS, *TO_PLAN]) == (
        "the Plan of the Billing version valid at the time of the Team of any of the Member's "
        "Member events"
    )
