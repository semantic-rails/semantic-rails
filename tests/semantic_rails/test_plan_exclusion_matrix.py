"""Every item an exclusion names is read, and every question that excludes values holds.

The planner doesn't answer exclusions yet, whatever the draft carries. An exclusion keeps rows
with no recorded value, so a hand-written ``IS DISTINCT FROM`` filter is its executable form:
"signups excluding web" counts the store signups and the one with no channel. That form is
compared with independent DuckDB SQL.
"""

from __future__ import annotations

import itertools
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.planner.exclusions import exclusion_clauses, exclusion_gaps
from semantic_rails.planner.time_windows import _time_window
from semantic_rails.runtime import Runtime
from semantic_rails.schema import ValueDomainConfig, ValueDomainValue
from tests.semantic_rails.conftest import opened
from tests.semantic_rails.result_helpers import assert_plan_held
from tests.semantic_rails.test_plan_accuracy_guard import _draft_plan

CHANNEL = "dimension.shop_customer_channel"
CUSTOMER = "dimension.shop_customer_id"
NOW = {"now": "2024-07-05T06:00:00Z"}
KEEPS = "IS DISTINCT FROM"
SHOP = Path(__file__).resolve().parents[1] / "integration" / "correctness" / "shop"
# Seed signups: web 101, 103, 106 (June 25, 2024); store 102, 104, 107 (June 10, 2024);
# 105 has no channel.
JUNE_2024 = "signed_up_at >= TIMESTAMP '2024-06-01' AND signed_up_at < TIMESTAMP '2024-07-01'"


def _values(store_label: str = "Top", *extra: ValueDomainValue) -> list[ValueDomainValue]:
    return [
        ValueDomainValue(value="web", label="Web"),
        ValueDomainValue(value="store", label=store_label),
        ValueDomainValue(value="partner", label="Partner"),
        *extra,
    ]


def _open(tmp_path: Path, values: list[ValueDomainValue], seed: str = "") -> Runtime:
    package = tmp_path / "shop"
    shutil.copytree(SHOP, package)
    if seed:
        script = package / "data" / "seed.sql"
        script.write_text(script.read_text() + seed)
    runtime = opened(Runtime.from_path(str(package)))
    runtime._config = replace(
        runtime._config,
        value_domains=[
            ValueDomainConfig(id="value_domain.channels", dimensions=[CHANNEL], values=values)
        ],
    )
    return runtime


@pytest.fixture()
def shop(tmp_path):
    runtime = _open(tmp_path, _values())
    try:
        yield runtime
    finally:
        runtime.close()


def _reference(runtime: Runtime, where: str) -> set[int]:
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        rows = connection.execute(f"SELECT customer_id FROM signups WHERE {where}").fetchall()
    return {row[0] for row in rows}


def _drops(*values: str, op: str = KEEPS) -> list[dict[str, Any]]:
    return [{"field": CHANNEL, "op": op, "value": value} for value in values]


def _signups(where: list[dict[str, Any]]) -> dict[str, Any]:
    select = [{"as": "signup_count", "expression": {"measure": "measure.shop.signup_count"}}]
    return {"version": 1, "select": select, "where": where, "policy_context": NOW}


def _plan(runtime: Runtime, question: str, where: list[dict[str, Any]] | None = None):
    partial: dict[str, Any] = {"policy_context": NOW}
    if where is not None:
        partial["where"] = where
    return plan_payload(runtime, intent=question, partial_query=partial)


def _selected(runtime: Runtime, query: dict[str, Any]) -> tuple[set[int], int]:
    """The customers ``query`` selects, and its total."""

    total = sum(row["signup_count"] for row in runtime.query(query)["rows"])
    by_customer = {**query, "group_by": [*query.get("group_by", []), CUSTOMER]}
    return {row[CUSTOMER] for row in runtime.query(by_customer)["rows"]}, total


def _assert_held(payload: dict[str, Any], kind: str = "negation_unrealized") -> None:
    assert_plan_held(payload, "PLAN_INTENT_COVERAGE_GAP")
    assert "execute" not in payload["next"].get("ready_for", [])
    kinds = {gap["kind"] for gap in payload["why"]["details"]["gaps"]}
    assert kind in kinds, kinds


def _items(runtime: Runtime, question: str) -> list[tuple[str, str]]:
    window = _time_window(question, policy_context=NOW)
    return [
        (item.kind, item.text)
        for clause in exclusion_clauses(runtime._config, question, window)
        for item in clause.items
    ]


def _gaps(runtime: Runtime, question: str, query: dict[str, Any], **kwargs: Any):
    window = _time_window(question, policy_context=NOW)
    return exclusion_gaps(runtime._config, question, query, window, **kwargs)


# --- the list an exclusion names ----------------------------------------------

MARKERS = [
    "signups excluding {}",
    "signups except {}",
    "signups except for {}",
    "signups without {}",
    "signups not from {}",
    "signups, but not {}",
    "signups other than {}",
    "signups apart from {}",
    "signups aside from {}",
    "signups minus {}",
    "signups outside of {}",
    "signups for all channels but {}",
]
LISTS = [
    "{} and {}",
    "{}, {}",
    "{}, and {}",
    "{} or {}",
    "{} nor {}",
    "{} & {}",
    "{} / {}",
    "{}; {}",
    "{} plus {}",
    "{} as well as {}",
    "{} along with {}",
    "{} alongside {}",
    "{} together with {}",
    "{} — {}",
    "{}\n{}",
    "({}, {})",
]
# Two items, and how the second one's item reads.
SPELLINGS = [("web", "Top", "Top"), ("Web", '"Top"', '"Top"'), ("the web", "stores", "stores")]


@pytest.mark.parametrize(
    ("marker", "form", "spelling"), list(itertools.product(MARKERS, LISTS, SPELLINGS))
)
def test_every_list_form_reads_each_item(shop, marker, form, spelling):
    question = marker.format(form.format(*spelling[:2]))
    assert [kind for kind, _text in _items(shop, question)] == ["value", "value"], question
    # Even the exact null-keeping exclusion of both items holds.
    [gap] = _gaps(shop, question, {"where": _drops("web", "store")})
    assert gap.kind == "negation_unrealized"
    assert [item["value"] for item in gap.expected["items"]] == ["web", "store"]
    assert gap.expected["items"][1]["text"] == spelling[2]


@pytest.mark.parametrize(
    ("question", "items"),
    [
        # A dotted date is one item, whole; a day without a year is read with its lead and
        # weekday.
        ("signups excluding Jun. 25, 2024", [("time", "Jun. 25, 2024")]),
        ("signups not on Jun. 25", [("time", "on Jun. 25")]),
        ("signups not on Tue. June 25", [("time", "on Tue. June 25")]),
        ("signups excluding Sept. 30", [("time", "Sept. 30")]),
        ("signups excluding Dec. 31, 2023", [("time", "Dec. 31, 2023")]),
        ("signups not in June 2024", [("time", "June 2024")]),
        ("signups not on June 25, 2024", [("time", "on June 25, 2024")]),
        ("signups excluding last month", [("time", "last month")]),
        ("signups excluding Q2", [("time", "Q2")]),
        ("signups excluding web and June 2024", [("value", "web"), ("time", "June 2024")]),
        # A bare month names no window the planner reads: unknown, never skipped.
        ("signups excluding June", [("unknown", "June")]),
        # The first time phrase after the list, across words only, stays positive.
        ("signups excluding web in June 2024", [("value", "web")]),
        ("signups excluding web signups last month", [("value", "web")]),
        ("signups in June 2024 excluding web", [("value", "web")]),
        # A separator before it puts it in the clause.
        ("signups excluding web, in June 2024", [("value", "web"), ("time", "June 2024")]),
        # A word that is no value in an item's place, or a mention past the list's end.
        ("signups excluding web and wholesale", [("value", "web"), ("unknown", "wholesale")]),
        (
            "signups excluding web stores and Top",
            [("value", "web"), ("unknown", "stores"), ("unknown", "Top")],
        ),
        ('signups excluding web channels, "A"', [("value", "web"), ("unknown", '"A"')]),
        ("signups excluding refunds", [("unknown", "refunds")]),
        ("signups excluding", [("unknown", "excluding")]),
        # An explicit inclusion ends the clause.
        ("signups excluding web, including Top", [("value", "web")]),
        ("signups not including web", [("value", "web")]),
    ],
)
def test_items_are_typed_and_never_dropped(shop, question, items):
    assert _items(shop, question) == items


def test_an_exclusion_window_is_never_a_positive_window():
    excluded = _time_window("signups not in June 2024", policy_context=NOW)
    assert excluded.bounds == {}
    assert excluded.unresolved == ("june 2024",)
    kept = _time_window("signups excluding web in June 2024", policy_context=NOW)
    assert kept.bounds == {"start": "2024-06-01", "end": "2024-07-01"}
    both = _time_window("signups in 2024 not in June 2024", policy_context=NOW)
    assert (both.bounds, both.unresolved) == ({}, ("june 2024",))


@pytest.mark.parametrize(
    ("label", "question"),
    [
        # A one-character name, or one that normalizes to no word, is still an item.
        ("A", 'signups excluding web and "A"'),
        ("A", "signups excluding web and A"),
        ("+", 'signups excluding web and "+"'),
        ("+", "signups excluding web, +"),
    ],
)
def test_short_and_symbol_names_are_items(tmp_path, monkeypatch, label, question):
    runtime = _open(tmp_path, _values(label))
    try:
        assert [kind for kind, _text in _items(runtime, question)] == ["value", "value"]
        assert len(_gaps(runtime, question, {"where": _drops("web")})) == 1
        # The planner drafts the second item too, and holds.
        payload = _plan(runtime, question, _drops("web"))
        assert payload["best"]["query_ir"]["where"] == _drops("web", "store")
        _assert_held(payload)
        _draft_plan(monkeypatch, _signups(_drops("web")))
        _assert_held(_plan(runtime, question))
    finally:
        runtime.close()


BOTH = "channel IS DISTINCT FROM 'web' AND channel IS DISTINCT FROM 'store'"


@pytest.mark.parametrize(
    ("label", "question", "items"),
    [
        # A character no item, separator or lead reads is an unknown item, never skipped.
        ("-", "signups excluding web and '-'", [("value", "web"), ("unknown", "'-'")]),
        ("-", "signups excluding web and ‘-’", [("value", "web"), ("unknown", "‘-’")]),
        ("-", "signups excluding web, -", [("value", "web"), ("unknown", "-")]),
        ("_", "signups excluding web, _", [("value", "web"), ("unknown", "_")]),
        (
            "Top",
            "signups excluding web and 'store'",
            [("value", "web"), ("unknown", "'"), ("value", "store"), ("unknown", "'")],
        ),
        # An opening curly quote is a mark that ends the list.
        (
            "Top",
            "signups excluding web and ‘store’",
            [("value", "web"), ("unknown", "‘"), ("unknown", "store")],
        ),
        # Only the question's own final mark is read.
        ("Top", "signups excluding web.", [("value", "web")]),
        ("Top", "signups excluding web? ", [("value", "web")]),
        ("Top", "signups excluding web!", [("value", "web")]),
        ("Top", "signups excluding web. by month", [("value", "web"), ("unknown", ".")]),
        ("Top", "signups excluding (web, Top)", [("value", "web"), ("value", "Top")]),
        ("Top", "signups not on Tue. June 25", [("time", "on Tue. June 25")]),
    ],
)
def test_every_character_of_a_list_is_read(tmp_path, label, question, items):
    runtime = _open(tmp_path, _values(label))
    try:
        assert _items(runtime, question) == items
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("label", "question"),
    [
        ("-", "signups excluding web and '-'"),
        ("-", "signups excluding web and ‘-’"),
        ("-", "signups excluding web, -"),
        ("_", "signups excluding web, _"),
        ("Top", "signups excluding web and 'store'"),
        ("Top", "signups excluding web and ‘store’"),
    ],
)
def test_an_unread_list_character_holds(tmp_path, monkeypatch, label, question):
    # The planner dropped only web here: 4, where the reference drops both values.
    runtime = _open(tmp_path, _values(label))
    try:
        assert _reference(runtime, BOTH) == {105}
        _assert_held(_plan(runtime, question))
        for where in (_drops("web"), _drops("web", op="!="), _drops("web", "store")):
            _draft_plan(monkeypatch, _signups(where))
            _assert_held(_plan(runtime, question))
    finally:
        runtime.close()


@pytest.mark.parametrize("question", ["signups excluding web.", "signups excluding web?"])
def test_a_question_ending_in_a_mark_holds(shop, monkeypatch, question):
    _assert_held(_plan(shop, question))
    _draft_plan(monkeypatch, _signups(_drops("web")))
    _assert_held(_plan(shop, question))


@pytest.mark.parametrize("label", ["Including Top", "Includes Top", "Include", "All but Web"])
@pytest.mark.parametrize("quote", ['"', ""])
def test_a_marker_inside_a_name_holds(tmp_path, monkeypatch, label, quote):
    # Read as a marker, the name ended the list early or opened a clause of its own: the
    # planner drafted '= store' (3) or dropped web alone (4).
    question = f"signups excluding web and {quote}{label}{quote}"
    runtime = _open(tmp_path, _values(label))
    try:
        assert _reference(runtime, BOTH) == {105}
        assert _items(runtime, question) == [("unknown", question)]
        _assert_held(_plan(runtime, question))
        for where in (
            [*_drops("web"), {"field": CHANNEL, "op": "=", "value": "store"}],
            _drops("web"),
            _drops("web", "store"),
        ):
            _draft_plan(monkeypatch, _signups(where))
            _assert_held(_plan(runtime, question))
    finally:
        runtime.close()


def test_a_name_spans_a_separator_only_when_it_contains_it(tmp_path, monkeypatch):
    runtime = _open(tmp_path, _values("Mall", ValueDomainValue(value="wm", label="Web Mall")))
    try:
        assert _items(runtime, "signups excluding web, mall") == [
            ("value", "web"),
            ("value", "mall"),
        ]
        assert _items(runtime, "signups excluding web mall") == [("value", "web mall")]
        _assert_held(_plan(runtime, "signups excluding web, mall", _drops("web", "store")))
        _draft_plan(monkeypatch, _signups(_drops("wm")))
        _assert_held(_plan(runtime, "signups excluding web, mall"))
    finally:
        runtime.close()


# --- every draft beside an exclusion holds --------------------------------------

DRAFTS = {
    "exact": _drops("web", "store"),
    "exact, reordered": _drops("store", "web"),
    "!=": _drops("web", "store", op="!="),
    "NOT IN": [{"field": CHANNEL, "op": "NOT IN", "value": ["web", "store"]}],
    "superset": _drops("web", "store", "partner"),
    "one item": _drops("web"),
    "other value": _drops("partner"),
    "other dimension": [{"field": CUSTOMER, "op": KEEPS, "value": "web"}, *_drops("store")],
    "case": _drops("Web", "store"),
    "reversed": [{"field": CHANNEL, "op": "=", "value": "web"}, *_drops("store")],
    "kept": [{"field": CHANNEL, "op": "IN", "value": ["partner"]}, *_drops("web", "store")],
    "none": [],
    "child scope": [
        *_drops("web"),
        {"child": "entity.shop_order", "match": "any", "where": _drops("store")},
    ],
}
QUESTIONS = [
    "signups excluding web and Top",
    "signups other than web, Top",
    'signups for all channels but web or "Top"',
    "signups excluding web; except Top",
    "signups excluding web and Top in June 2024",
]


@pytest.mark.parametrize(("question", "draft"), list(itertools.product(QUESTIONS, DRAFTS)))
def test_every_clause_holds_whatever_the_draft(shop, question, draft):
    # A positive filter that keeps an excluded value reverses the request.
    kind = "negation_reversed" if draft == "reversed" else "negation_unrealized"
    gaps = _gaps(shop, question, {"where": DRAFTS[draft], "policy_context": NOW})
    clauses = exclusion_clauses(shop._config, question, _time_window(question, policy_context=NOW))
    assert len(gaps) == len(clauses) >= 1
    assert gaps[0].kind == kind
    assert {gap.recovery_hint["kind"] for gap in gaps} == {"ask_for_breakdown"}
    if draft in {"exact", "reversed"}:
        _assert_held(_plan(shop, question, DRAFTS[draft]), kind)


@pytest.mark.parametrize(
    "question",
    [
        "signups not in June 2024",
        "signups not on June 25, 2024",
        "signups excluding Jun. 25, 2024",
        "signups not on Jun. 25",
        "signups not on Tue. June 25",
        "signups excluding last month",
        "signups excluding web and June 2024",
        "signups excluding web, in June 2024",
    ],
)
@pytest.mark.parametrize("where", [_drops("store", op="!="), _drops("store"), _drops("web")])
def test_a_time_exclusion_always_holds(shop, question, where):
    # Query IR has no window complement, whatever else the draft drops. The reference
    # for "not in June 2024" beside "!= store" is 2; the June window alone gave 1.
    assert _reference(shop, f"channel <> 'store' AND NOT ({JUNE_2024})") == {101, 103}
    payload = _plan(shop, question, where)
    _assert_held(payload)
    gap = next(g for g in payload["why"]["details"]["gaps"] if g["kind"] == "negation_unrealized")
    assert "time" in {item["kind"] for item in gap["expected"]["items"]}
    # The windows before and after the excluded one would drop rows with no time: the hold
    # suggests a breakdown, never a list of what to keep.
    hints = " ".join(hint["message"] for hint in payload["why"]["recovery_hints"])
    assert "breakdown" in hints
    assert "before and after" not in hints


# --- the questions that were answered with a wrong number -----------------------


@pytest.mark.parametrize(
    ("question", "second"),
    [
        ("signups excluding web and Top", "store"),
        ("signups excluding web, Top", "store"),
        ("signups excluding web or Top", "store"),
        ('signups excluding web and "Top"', "store"),
        ('signups excluding web alongside "A"', "store"),
        ('signups excluding web together with "A"', "store"),
        ('signups excluding web; "A"', "store"),
        ('signups excluding web as well as "A"', "store"),
        ('signups excluding web plus "A"', "store"),
        ('signups excluding web along with "A"', "store"),
        ('signups excluding web — "A"', "store"),
        ('signups excluding web\n"A"', "store"),
        ('signups excluding (web, "A")', "store"),
        ("signups excluding web and partner", "partner"),
    ],
)
def test_a_partly_dropped_list_holds(tmp_path, monkeypatch, question, second):
    runtime = _open(tmp_path, _values("A" if '"A"' in question else "Top"))
    try:
        # The planner drafts every item, and holds; so does the caller's '!='.
        payload = _plan(runtime, question, _drops("web"))
        assert payload["best"]["query_ir"]["where"] == _drops("web", second)
        _assert_held(payload)
        _assert_held(_plan(runtime, question, _drops("web", op="!=")))
        # A draft that drops only the first item holds, though it validates.
        _draft_plan(monkeypatch, _signups(_drops("web")))
        _assert_held(_plan(runtime, question))
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "question",
    [
        "signups excluding web and Top",
        "signups excluding web, and Top",
        "signups excluding web, Top",
        "signups excluding web / Top",
        "signups excluding web & Top",
        "signups excluding web or Top",
        'signups excluding web and "Top"',
    ],
)
def test_a_list_dropped_exactly_holds(shop, question):
    _assert_held(_plan(shop, question, _drops("web", "store")))


def test_an_excluded_value_named_top_is_no_ranking(shop):
    # Read as "top 5", it drafted a limit, which truncates a grouped answer.
    payload = _plan(shop, "signups excluding web and Top")
    assert "limit" not in payload["best"]["query_ir"]
    assert payload["best"]["query_ir"]["where"] == _drops("web", "store")
    _assert_held(payload)


def test_the_held_draft_is_the_null_keeping_exclusion(shop):
    # The held draft shows the executable form, and it runs as reference SQL counts.
    payload = _plan(shop, "signups excluding web")
    _assert_held(payload)
    assert payload["best"]["query_ir"]["where"] == _drops("web")
    expected = _reference(shop, "channel IS DISTINCT FROM 'web'")
    assert _selected(shop, payload["best"]["query_ir"]) == (expected, 4)
    windowed = _plan(shop, "signups excluding web in June 2024")
    _assert_held(windowed)
    expected = _reference(shop, f"channel IS DISTINCT FROM 'web' AND {JUNE_2024}")
    assert _selected(shop, windowed["best"]["query_ir"]) == (expected, 1) == ({107}, 1)
    # A plain '!=' holds too.
    _assert_held(_plan(shop, "signups excluding web", _drops("web", op="!=")))


def test_an_exclusion_keeps_rows_with_no_recorded_value(tmp_path):
    seed = (
        "\nDELETE FROM signups;\nINSERT INTO signups VALUES "
        + ", ".join(
            f"({number}, TIMESTAMP '2024-05-0{number % 9 + 1} 09:00:00', {channel})"
            for number, channel in enumerate(
                ["'web'"] * 5 + ["'store'"] * 3 + ["NULL"] * 2, start=201
            )
        )
        + ";\n"
    )
    runtime = _open(tmp_path, _values(), seed)
    try:
        # A hand-written Query IR with IS DISTINCT FROM runs and keeps the 2 with no channel;
        # '!=' drops them.
        expected = _reference(runtime, "channel IS DISTINCT FROM 'web'")
        assert len(expected) == 5
        assert _selected(runtime, _signups(_drops("web"))) == (expected, 5)
        assert runtime.query(_signups(_drops("web", op="!=")))["rows"] == [{"signup_count": 3}]
        for op in (KEEPS, "!="):
            _assert_held(_plan(runtime, "signups excluding web", _drops("web", op=op)))
    finally:
        runtime.close()


def test_a_caller_constraint_never_discharges_an_item(shop):
    question = "signups excluding web"
    [gap] = _gaps(shop, question, {"where": _drops("store")})
    assert gap.kind == "negation_unrealized"
    _assert_held(_plan(shop, question, _drops("store")))


SIGNUPS = {"measure": "measure.shop.signup_count"}


@pytest.mark.parametrize(
    "control",
    [
        lambda query: query,
        lambda query: {**query, "order_by": [{"field": "signup_count", "direction": "DESC"}]},
        lambda query: {**query, "_note": "drafted for a test"},
        lambda query: {**query, "verbosity": "minimal"},
        lambda query: {
            **query,
            "select": [{**query["select"][0], "expression": {"kind": "measure", **SIGNUPS}}],
        },
    ],
    ids=["canonical", "order by the alias", "annotation", "verbosity", "kind measure"],
)
@pytest.mark.parametrize(
    "question", ["signups excluding web", "signups excluding web in June 2024"]
)
def test_an_exact_exclusion_draft_holds(shop, monkeypatch, question, control):
    _draft_plan(monkeypatch, control(_plan(shop, question)["best"]["query_ir"]))
    _assert_held(_plan(shop, question))


def test_a_value_named_outside_the_exclusion_holds(shop, monkeypatch):
    # Mixed questions aren't read yet: "store" keeps a value beside the exclusion.
    question = "store signups excluding web"
    _assert_held(_plan(shop, question))
    _draft_plan(monkeypatch, _signups([{"field": CHANNEL, "op": "=", "value": "store"}]))
    assert _plan(shop, question)["status"] != "ok"
    _draft_plan(
        monkeypatch, _signups([*_drops("web"), {"field": CHANNEL, "op": "=", "value": "store"}])
    )
    _assert_held(_plan(shop, question))


STORE = "dimension.jaffle_store_name"


@pytest.mark.parametrize(
    "question",
    [
        # Each one was ready with one total: the grouping read "store excluding brooklyn".
        "revenue by store excluding Brooklyn",
        "revenue by store not Brooklyn",
        "orders by store except New Orleans",
        # Ready by month alone, without the store.
        "revenue by month and store excluding Brooklyn",
        "revenue by month excluding Brooklyn",
    ],
)
def test_an_exclusion_inside_a_grouping_phrase_holds(runtime_factory, question):
    runtime = runtime_factory("jaffle_shop")
    try:
        window = _time_window(question)
        assert [
            item.text
            for clause in exclusion_clauses(runtime._config, question, window)
            for item in clause.items
        ] == [question]
        _assert_held(plan_payload(runtime, intent=question))
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("package", "question"),
    [
        # One total, without the store column the question may ask for.
        ("jaffle_shop", "revenue across stores excluding Brooklyn"),
        ("jaffle_shop", "store revenue excluding Brooklyn"),
        # The store column dropped, one total.
        ("jaffle_shop", "revenue by store without Brooklyn"),
        ("jaffle_shop", "revenue by store other than Brooklyn"),
        ("jaffle_shop", "revenue by store but not Brooklyn"),
        ("jaffle_shop", "revenue grouped by store excluding Brooklyn"),
        ("shop", "signups excluding web"),
        ("shop", "signups excluding web in June 2024"),
    ],
)
def test_a_question_that_excludes_values_holds(runtime_factory, tmp_path, package, question):
    runtime = runtime_factory(package) if package != "shop" else _open(tmp_path, _values())
    try:
        payload = plan_payload(runtime, intent=question, partial_query={"policy_context": NOW})
        _assert_held(payload)
        assert "ask_for_breakdown" in {hint["kind"] for hint in payload["why"]["recovery_hints"]}
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("question", "excluded"),
    [
        ("revenue excluding Brooklyn", "Brooklyn"),
        ("revenue not Brooklyn by month", "Brooklyn"),
        ("revenue for all stores except New Orleans", "New Orleans"),
    ],
)
def test_held_store_exclusion_drafts_match_reference_sql(runtime_factory, question, excluded):
    # These hold. The held draft is the null-keeping exclusion, which keeps orders whose store
    # has no name, and run by hand it matches reference SQL.
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=question)
        _assert_held(payload)
        query = payload["best"]["query_ir"]
        assert query["where"] == [{"field": STORE, "op": KEEPS, "value": excluded}]
        alias = query["select"][0]["as"]
        total = sum(row[alias] for row in runtime.query(query)["rows"])
        overall = {key: value for key, value in query.items() if key not in {"time", "order_by"}}
        by_store = {
            row[STORE]: row[alias]
            for row in runtime.query({**overall, "group_by": [STORE]})["rows"]
        }
    finally:
        runtime.close()
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        expected = dict(
            connection.execute(
                "SELECT s.store_name, SUM(o.order_total_cents / 100.0) FROM jaffle_order o "
                "LEFT JOIN jaffle_store s ON o.store_id = s.store_id "
                "WHERE s.store_name IS DISTINCT FROM ? GROUP BY 1",
                [excluded],
            ).fetchall()
        )
    assert excluded not in expected
    assert by_store == pytest.approx(expected)
    assert total == pytest.approx(sum(expected.values()))
