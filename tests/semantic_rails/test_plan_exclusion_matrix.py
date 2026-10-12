"""Every item an exclusion names is read; a question is answered when the draft leaves every
excluded value out with ``not``, and held otherwise.

An exclusion keeps rows with no recorded value, as ``not`` does: "signups excluding web" counts
the store signups and the one with no channel. A time phrase an exclusion names (a window has no
complement), an item the reader can't read, or values the draft leaves out differently hold the
plan. Every answer is compared with independent DuckDB SQL.
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
from semantic_rails.planner.call import Call
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
KEEPS = "not"
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


@pytest.fixture(scope="module")
def shop(tmp_path_factory):
    runtime = _open(tmp_path_factory.mktemp("exclusions"), _values())
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


def _dropped(*values: str) -> list[dict[str, Any]]:
    """The planner's form: one ``not`` naming every excluded value."""
    return [
        {"field": CHANNEL, "op": KEEPS, "value": list(values) if len(values) > 1 else values[0]}
    ]


def _signups(where: list[dict[str, Any]]) -> dict[str, Any]:
    return {"select": ["measure.shop.signup_count"], "where": where}


_signups_draft = _signups


def _plan(runtime: Runtime, question: str, where: list[dict[str, Any]] | None = None):
    partial = {"where": where} if where is not None else None
    return plan_payload(runtime, intent=question, partial_query=partial, now=NOW["now"])


def _selected(runtime: Runtime, query: dict[str, Any]) -> tuple[set[int], int]:
    """The customers ``query`` selects, and its total."""

    total = sum(row["signup_count"] for row in runtime.query(query, now=NOW["now"])["rows"])
    by_customer = {**query, "group_by": [*query.get("group_by", []), CUSTOMER]}
    rows = runtime.query(by_customer, now=NOW["now"])["rows"]
    return {row[CUSTOMER] for row in rows}, total


def _assert_held(payload: dict[str, Any], kind: str = "negation_unrealized") -> None:
    assert_plan_held(payload, "PLAN_INTENT_COVERAGE_GAP")
    assert "execute" not in payload["next"].get("ready_for", [])
    kinds = {gap["kind"] for gap in payload["why"]["details"]["gaps"]}
    assert kind in kinds, kinds


def _assert_answered(runtime: Runtime, payload: dict[str, Any], where: str) -> None:
    """Ready, and the signups it counts are the ones reference SQL selects."""
    assert payload["status"] == "ok", payload.get("why")
    assert "execute" in payload["next"]["ready_for"]
    expected = _reference(runtime, where)
    assert _selected(runtime, payload["best"]["query"]) == (expected, len(expected))


def _items(runtime: Runtime, question: str) -> list[tuple[str, str]]:
    window = _time_window(question, policy_context=NOW)
    return [
        (item.kind, item.text)
        for clause in exclusion_clauses(runtime._config, question, window)
        for item in clause.items
    ]


def _gaps(runtime: Runtime, question: str, where: list[dict[str, Any]]):
    window = _time_window(question, policy_context=NOW)
    return exclusion_gaps(runtime._config, question, Call.from_call({"where": where}), window)


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
    # Leaving both items out answers the clause; leaving out one holds it.
    assert _gaps(shop, question, _drops("web", "store")) == []
    assert _gaps(shop, question, _dropped("web", "store")) == []
    [gap] = _gaps(shop, question, _drops("web"))
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
        assert len(_gaps(runtime, question, _drops("web"))) == 1
        # The planner leaves the second item out too, and answers.
        payload = _plan(runtime, question, _drops("web"))
        assert payload["best"]["query"]["where"] == [*_drops("web"), *_dropped("web", "store")]
        _assert_answered(runtime, payload, BOTH)
        _draft_plan(monkeypatch, _signups_draft(_drops("web")))
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
            _draft_plan(monkeypatch, _signups_draft(where))
            _assert_held(_plan(runtime, question))
    finally:
        runtime.close()


@pytest.mark.parametrize("question", ["signups excluding web.", "signups excluding web?"])
def test_a_question_ending_in_a_mark_is_answered(shop, monkeypatch, question):
    _assert_answered(shop, _plan(shop, question), "channel IS DISTINCT FROM 'web'")
    _draft_plan(monkeypatch, _signups_draft(_drops("web")))
    _assert_answered(shop, _plan(shop, question), "channel IS DISTINCT FROM 'web'")


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
            [*_drops("web"), {"field": CHANNEL, "op": "is", "value": "store"}],
            _drops("web"),
            _drops("web", "store"),
        ):
            _draft_plan(monkeypatch, _signups_draft(where))
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
        payload = _plan(runtime, "signups excluding web, mall", _drops("web", "store"))
        _assert_answered(runtime, payload, BOTH)
        _draft_plan(monkeypatch, _signups_draft(_drops("wm")))
        _assert_held(_plan(runtime, "signups excluding web, mall"))
    finally:
        runtime.close()


# --- a draft answers an exclusion only by leaving out exactly its values ---------

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
    "reversed": [{"field": CHANNEL, "op": "is", "value": "web"}, *_drops("store")],
    "kept": [{"field": CHANNEL, "op": "is", "value": ["partner"]}, *_drops("web", "store")],
    "none": [],
    "child scope": [*_drops("web"), {"entity": "entity.shop_order", "any": _drops("store")}],
}
# Drafts that leave out web and store, and nothing else of the channel.
ANSWERING = {"exact", "exact, reordered", "!=", "NOT IN"}
QUESTIONS = [
    "signups excluding web and Top",
    "signups other than web, Top",
    'signups for all channels but web or "Top"',
    "signups excluding web; except Top",
    "signups excluding web and Top in June 2024",
]


@pytest.mark.parametrize(("question", "draft"), list(itertools.product(QUESTIONS, DRAFTS)))
def test_a_clause_is_answered_only_by_leaving_out_exactly_its_values(shop, question, draft):
    gaps = _gaps(shop, question, DRAFTS[draft])
    clauses = exclusion_clauses(shop._config, question, _time_window(question, policy_context=NOW))
    assert len(clauses) >= 1
    if draft in ANSWERING:
        assert gaps == []
        return
    # A positive filter that keeps an excluded value reverses the request.
    kind = "negation_reversed" if draft == "reversed" else "negation_unrealized"
    assert len(gaps) == len(clauses)
    assert gaps[0].kind == kind
    assert {gap.recovery_hint["kind"] for gap in gaps} == {"ask_for_breakdown"}
    if draft == "reversed":
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


# --- the questions that were answered with a wrong number, and now aren't ----------


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
def test_a_partly_dropped_list_is_completed_or_held(tmp_path, monkeypatch, question, second):
    runtime = _open(tmp_path, _values("A" if '"A"' in question else "Top"))
    both = f"channel IS DISTINCT FROM 'web' AND channel IS DISTINCT FROM '{second}'"
    try:
        # The planner leaves every item out beside the caller's, and answers.
        payload = _plan(runtime, question, _drops("web"))
        assert payload["best"]["query"]["where"] == [*_drops("web"), *_dropped("web", second)]
        _assert_answered(runtime, payload, both)
        _assert_answered(runtime, _plan(runtime, question, _drops("web", op="!=")), both)
        # A draft that drops only the first item holds, though it validates.
        _draft_plan(monkeypatch, _signups_draft(_drops("web")))
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
def test_a_list_dropped_exactly_is_answered(shop, question):
    _assert_answered(shop, _plan(shop, question, _drops("web", "store")), BOTH)


def test_an_excluded_value_named_top_is_no_ranking(shop):
    # Read as "top 5", it drafted a limit, which truncates a grouped answer.
    payload = _plan(shop, "signups excluding web and Top")
    assert "limit" not in payload["best"]["query"]
    assert payload["best"]["query"]["where"] == _dropped("web", "store")
    _assert_answered(shop, payload, BOTH)


def test_an_exclusion_is_answered_with_the_null_keeping_not(shop):
    # Held before, though its draft counted these 4: the answer is the executable form.
    payload = _plan(shop, "signups excluding web")
    assert payload["best"]["query"]["where"] == _drops("web")
    _assert_answered(shop, payload, "channel IS DISTINCT FROM 'web'")
    assert _selected(shop, payload["best"]["query"])[1] == 4
    windowed = _plan(shop, "signups excluding web in June 2024")
    _assert_answered(shop, windowed, f"channel IS DISTINCT FROM 'web' AND {JUNE_2024}")
    assert _selected(shop, windowed["best"]["query"]) == ({107}, 1)
    # A plain '!=' is the same word.
    payload = _plan(shop, "signups excluding web", _drops("web", op="!="))
    _assert_answered(shop, payload, "channel IS DISTINCT FROM 'web'")


def test_an_exclusion_keeps_rows_with_no_recorded_value(tmp_path):
    seed = (
        "\nDELETE FROM signups;\nINSERT INTO signups (customer_id, signed_up_at, channel) VALUES "
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
        # A call with ``not`` runs and keeps the 2 with no channel; '!=' is the same word.
        expected = _reference(runtime, "channel IS DISTINCT FROM 'web'")
        assert len(expected) == 5
        assert _selected(runtime, _signups(_drops("web"))) == (expected, 5)
        rows = runtime.query(_signups(_drops("web", op="!=")), now=NOW["now"])["rows"]
        assert rows == [{"signup_count": 5}]
        for op in (KEEPS, "!="):
            payload = _plan(runtime, "signups excluding web", _drops("web", op=op))
            _assert_answered(runtime, payload, "channel IS DISTINCT FROM 'web'")
    finally:
        runtime.close()


def test_a_caller_constraint_never_discharges_an_item(shop):
    question = "signups excluding web"
    [gap] = _gaps(shop, question, _drops("store"))
    assert gap.kind == "negation_unrealized"
    # The caller also leaves out store, which the question doesn't: a different question.
    _assert_held(_plan(shop, question, _drops("store")))


SIGNUPS = {"kind": "measure", "measure": "measure.shop.signup_count"}


@pytest.mark.parametrize(
    "control",
    [
        lambda query: query,
        lambda query: {**query, "order_by": [{"field": "signup_count", "direction": "desc"}]},
        lambda query: {**query, "select": [{"as": "signup_count", "expression": SIGNUPS}]},
    ],
    ids=["canonical", "order by the alias", "kind measure"],
)
@pytest.mark.parametrize(
    ("question", "where"),
    [
        ("signups excluding web", "channel IS DISTINCT FROM 'web'"),
        (
            "signups excluding web in June 2024",
            f"channel IS DISTINCT FROM 'web' AND {JUNE_2024}",
        ),
    ],
)
def test_an_exact_exclusion_draft_is_answered(shop, monkeypatch, question, where, control):
    _draft_plan(monkeypatch, control(_plan(shop, question)["best"]["query"]))
    _assert_answered(shop, _plan(shop, question), where)


def test_a_value_named_outside_the_exclusion_holds(shop, monkeypatch):
    # Mixed questions aren't read yet: "store" keeps a value beside the exclusion.
    question = "store signups excluding web"
    _assert_held(_plan(shop, question))
    _draft_plan(monkeypatch, _signups_draft([{"field": CHANNEL, "op": "is", "value": "store"}]))
    assert _plan(shop, question)["status"] != "ok"
    _draft_plan(
        monkeypatch,
        _signups_draft([*_drops("web"), {"field": CHANNEL, "op": "is", "value": "store"}]),
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
    "question",
    [
        # The store column dropped, one total: the grouping read "store ... Brooklyn".
        "revenue by store without Brooklyn",
        "revenue by store other than Brooklyn",
        "revenue by store but not Brooklyn",
        "revenue grouped by store excluding Brooklyn",
    ],
)
def test_an_exclusion_after_a_grouping_holds(runtime_factory, question):
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=question)
        _assert_held(payload)
        assert "ask_for_breakdown" in {hint["kind"] for hint in payload["why"]["recovery_hints"]}
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("question", "excluded", "grain"),
    [
        ("revenue excluding Brooklyn", "Brooklyn", ""),
        ("revenue across stores excluding Brooklyn", "Brooklyn", ""),
        ("store revenue excluding Brooklyn", "Brooklyn", ""),
        ("revenue not Brooklyn by month", "Brooklyn", "month"),
        ("revenue for all stores except New Orleans", "New Orleans", ""),
    ],
)
def test_a_store_exclusion_is_answered_as_reference_sql(runtime_factory, question, excluded, grain):
    # Held before. The answer leaves the store out with ``not``, which keeps orders whose store
    # has no name, and it matches reference SQL.
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=question)
        assert payload["status"] == "ok", payload.get("why")
        query = payload["best"]["query"]
        assert query["where"] == [{"field": STORE, "op": KEEPS, "value": excluded}]
        assert query.get("time", {}).get("grain", "") == grain
        total = sum(row["revenue_usd"] for row in runtime.query(query)["rows"])
        overall = {key: value for key, value in query.items() if key not in {"time", "order_by"}}
        by_store = {
            row[STORE]: row["revenue_usd"]
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
