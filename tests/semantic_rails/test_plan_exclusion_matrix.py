"""Every item an exclusion names needs its own exact predicate, and a time exclusion holds.

An exclusion keeps rows with no recorded value, so only ``IS DISTINCT FROM`` realizes it:
"signups excluding web" counts the store signups and the one with no channel. Ready plans are
compared with independent DuckDB SQL by the customers they select and by their total.
"""

from __future__ import annotations

import itertools
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.planner.exclusions import _INERT_KEYS, exclusion_clauses, exclusion_gaps
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


def _assert_ready(runtime: Runtime, payload: dict[str, Any], expected: set[int]) -> None:
    """The plan executes, selecting exactly the reference's customers, and totals them."""

    assert payload["status"] == "ok", payload.get("why")
    assert payload["next"]["ready_for"] == ["execute"]
    query = payload["best"]["query_ir"]
    total = sum(row["signup_count"] for row in runtime.query(query)["rows"])
    by_customer = {**query, "group_by": [*query.get("group_by", []), CUSTOMER]}
    selected = {row[CUSTOMER] for row in runtime.query(by_customer)["rows"]}
    assert (selected, total) == (expected, len(expected))


def _assert_held(payload: dict[str, Any]) -> None:
    assert_plan_held(payload, "PLAN_INTENT_COVERAGE_GAP")
    assert "execute" not in payload["next"].get("ready_for", [])
    kinds = {gap["kind"] for gap in payload["why"]["details"]["gaps"]}
    assert kinds & {"negation_unrealized", "negation_reversed"}, kinds


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
    gaps = _gaps(shop, question, {"where": _drops("web", "store")})
    assert gaps == [], question
    [gap] = _gaps(shop, question, {"where": _drops("web")})
    assert gap.actual["missing"] == [spelling[2]]


@pytest.mark.parametrize(
    ("question", "items"),
    [
        # A dotted date is one item, whole; a weekday leads into its date.
        ("signups excluding Jun. 25, 2024", [("time", "Jun. 25, 2024")]),
        ("signups not on Jun. 25", [("time", "Jun. 25")]),
        ("signups not on Tue. June 25", [("time", "June 25")]),
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
        # The planner drafts the second item too.
        payload = _plan(runtime, question, _drops("web"))
        _assert_ready(runtime, payload, _reference(runtime, "channel IS NULL"))
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
        ("Top", "signups not on Tue. June 25", [("time", "June 25")]),
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
def test_the_questions_final_mark_keeps_it_ready(shop, monkeypatch, question):
    expected = _reference(shop, "channel IS DISTINCT FROM 'web'")
    assert len(expected) == 4
    _assert_ready(shop, _plan(shop, question), expected)
    _draft_plan(monkeypatch, _signups(_drops("web")))
    _assert_ready(shop, _plan(shop, question), expected)


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
        _assert_ready(
            runtime,
            _plan(runtime, "signups excluding web, mall", _drops("web", "store")),
            _reference(runtime, "channel IS NULL"),
        )
        _draft_plan(monkeypatch, _signups(_drops("wm")))
        _assert_held(_plan(runtime, "signups excluding web, mall"))
    finally:
        runtime.close()


# --- what realizes an exclusion -------------------------------------------------

DRAFTS = {
    "exact": (_drops("web", "store"), True),
    "exact, reordered": (_drops("store", "web"), True),
    "!=": (_drops("web", "store", op="!="), False),
    "NOT IN": ([{"field": CHANNEL, "op": "NOT IN", "value": ["web", "store"]}], False),
    "superset": (_drops("web", "store", "partner"), False),
    "one item": (_drops("web"), False),
    "other value": (_drops("partner"), False),
    "other dimension": (
        [{"field": CUSTOMER, "op": KEEPS, "value": "web"}, *_drops("store")],
        False,
    ),
    "case": (_drops("Web", "store"), False),
    "reversed": ([{"field": CHANNEL, "op": "=", "value": "web"}, *_drops("store")], False),
    "kept": (
        [{"field": CHANNEL, "op": "IN", "value": ["partner"]}, *_drops("web", "store")],
        False,
    ),
    "none": ([], False),
    "child scope": (
        [
            *_drops("web"),
            {"child": "entity.shop_order", "match": "any", "where": _drops("store")},
        ],
        False,
    ),
}
QUESTIONS = [
    "signups excluding web and Top",
    "signups other than web, Top",
    'signups for all channels but web or "Top"',
    "signups excluding web; except Top",
    "signups excluding web and Top in June 2024",
]


@pytest.mark.parametrize(("question", "draft"), list(itertools.product(QUESTIONS, DRAFTS)))
def test_only_an_exact_null_keeping_drop_of_every_item_is_ready(shop, question, draft):
    where, ready = DRAFTS[draft]
    gaps = _gaps(shop, question, {"where": where, "policy_context": NOW})
    assert (gaps == []) is ready
    if ready:
        window = f" AND {JUNE_2024}" if "June" in question else ""
        expected = _reference(
            shop,
            "channel IS DISTINCT FROM 'web' AND channel IS DISTINCT FROM 'store'" + window,
        )
        _assert_ready(shop, _plan(shop, question, where), expected)


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
    hints = " ".join(hint["message"] for hint in payload["why"]["recovery_hints"])
    assert "before and after" in hints
    assert gap["actual"]["unresolved"]


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
        # The planner drafts every item; the caller's '!=' still drops rows with no channel.
        expected = _reference(
            runtime, f"channel IS DISTINCT FROM 'web' AND channel IS DISTINCT FROM '{second}'"
        )
        _assert_ready(runtime, _plan(runtime, question, _drops("web")), expected)
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
def test_a_list_dropped_exactly_is_ready(shop, question):
    _assert_ready(
        shop, _plan(shop, question, _drops("web", "store")), _reference(shop, "channel IS NULL")
    )


def test_an_excluded_value_named_top_is_no_ranking(shop):
    # Read as "top 5", it drafted a limit, which truncates a grouped answer.
    payload = _plan(shop, "signups excluding web and Top")
    assert "limit" not in payload["best"]["query_ir"]
    _assert_ready(shop, payload, _reference(shop, BOTH))


def test_the_planner_drafts_the_null_keeping_exclusion(shop):
    payload = _plan(shop, "signups excluding web")
    assert payload["best"]["query_ir"]["where"] == _drops("web")
    _assert_ready(shop, payload, _reference(shop, "channel IS DISTINCT FROM 'web'"))
    windowed = _plan(shop, "signups excluding web in June 2024")
    expected = _reference(shop, f"channel IS DISTINCT FROM 'web' AND {JUNE_2024}")
    assert expected == {107}
    _assert_ready(shop, windowed, expected)
    # A plain '!=' drops the signup with no channel.
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
        expected = _reference(runtime, "channel IS DISTINCT FROM 'web'")
        assert len(expected) == 5
        _assert_ready(runtime, _plan(runtime, "signups excluding web", _drops("web")), expected)
        held = _plan(runtime, "signups excluding web", _drops("web", op="!="))
        _assert_held(held)
        assert runtime.query(held["best"]["query_ir"])["rows"] == [{"signup_count": 3}]
        gap = held["why"]["details"]["gaps"][0]
        assert gap["actual"]["drops_rows_without_a_value"] == ["where[0]"]
        assert any("IS DISTINCT FROM" in hint["message"] for hint in held["why"]["recovery_hints"])
    finally:
        runtime.close()


def test_a_caller_constraint_never_discharges_an_item(shop):
    question = "signups excluding web"
    gaps = _gaps(shop, question, {"where": _drops("store")})
    assert [gap.actual["missing"] for gap in gaps] == [["web"]]
    _assert_held(_plan(shop, question, _drops("store")))


def test_an_exclusion_inside_a_selected_expression_is_no_evidence(shop):
    question = "signups excluding web and Top"
    scoped = {
        "as": "signup_count",
        "expression": {
            "kind": "scoped_aggregate",
            "measure": "measure.shop.signup_count",
            "where": _drops("store"),
        },
    }
    [gap] = _gaps(shop, question, {"select": [scoped], "where": _drops("web")})
    assert "Top" in gap.actual["missing"]


# Scopes no exclusion names, each beside an exact exclusion draft.
NO_STORE_B = {
    "child": "entity.shop_order",
    "match": "none",
    "where": [{"field": "dimension.shop_order_store_id", "op": "=", "value": "b"}],
}
SCOPES = {
    # Drops the customers with a store-b order: 2 for "signups excluding web", not 4.
    "child group": ({"where": [NO_STORE_B]}, "where[{}]"),
    "or node": (
        {
            "where": [
                {
                    "op": "OR",
                    "args": [
                        {"field": CUSTOMER, "op": "=", "value": 102},
                        {"field": CUSTOMER, "op": "=", "value": 104},
                    ],
                }
            ]
        },
        "where[{}]",
    ),
    # Drops customer 102 inside the measure: 3, not 4.
    "filtered selected expression": (
        {
            "select": [
                {
                    "as": "signup_count",
                    "expression": {
                        "kind": "scoped_aggregate",
                        "measure": "measure.shop.signup_count",
                        "where": [{"field": CUSTOMER, "op": "!=", "value": 102}],
                    },
                }
            ]
        },
        "select[0]",
    ),
}
READY = {
    "signups excluding web": _drops("web"),
    **{question: _drops("web", "store") for question in QUESTIONS},
}


@pytest.mark.parametrize(("question", "scope"), list(itertools.product(READY, SCOPES)))
def test_adding_an_unrelated_scope_never_keeps_readiness(shop, monkeypatch, question, scope):
    where = READY[question]
    assert _gaps(shop, question, _signups(where)) == []
    change, path = SCOPES[scope]
    draft = _signups([*where, *change.get("where", [])])
    draft["select"] = change.get("select", draft["select"])
    excess = [row for gap in _gaps(shop, question, draft) for row in gap.actual["excess"]]
    assert [row["path"] for row in excess] == [path.format(len(where))]
    _draft_plan(monkeypatch, draft)
    payload = _plan(shop, question)
    assert "execute" not in payload["next"].get("ready_for", [])
    if scope != "or node":  # A compound node also fails validation.
        _assert_held(payload)


SIGNUPS = {"measure": "measure.shop.signup_count"}
SIGNED_UP = "temporal_role.shop_customer_signed_up_at"
FOUR = {"kind": "literal", "value": 4}
# Every select expression kind but a plain measure or metric reference, with its parts.
KINDS = {
    "measure_ref": SIGNUPS,
    "aggregate": SIGNUPS,
    "semi_additive": SIGNUPS,
    "scoped_aggregate": {**SIGNUPS, "where": [{"field": CUSTOMER, "op": "!=", "value": 102}]},
    "aggregate_if": {
        "aggregation": "count",
        "condition": {
            "kind": "comparison",
            "op": "!=",
            "left": {"kind": "column", "column": "channel", "entity": "entity.shop_customer"},
            "right": {"kind": "literal", "value": "web"},
        },
    },
    "arithmetic": {"op": "+", "left": SIGNUPS, "right": FOUR},
    "binary": {"op": "-", "left": SIGNUPS, "right": FOUR},
    "ratio": {"numerator": SIGNUPS, "denominator": SIGNUPS},
    "literal": {"value": 4},
    "prior_period": {"input": SIGNUPS, "offset": {"unit": "month", "value": 1}},
    "rolling": {"input": SIGNUPS, "window": {"unit": "day", "value": 7}},
    "cumulative": {"input": SIGNUPS},
    "period_to_date": {"input": SIGNUPS, "period": "month"},
    "conversion": {
        "base": SIGNUPS,
        "converted": SIGNUPS,
        "entity": "entity.shop_customer",
        "window": {"unit": "day", "value": 7},
        "matching_mode": "first_converted_after_base",
    },
    "distribution": {
        "function": "avg",
        "over": {"kind": "entity_value", "entity": "entity.shop_customer", "input": SIGNUPS},
    },
    "call": {"name": "abs", "args": [SIGNUPS]},
    "between": {"expr": SIGNUPS, "low": FOUR, "high": FOUR},
    "not_between": {"expr": SIGNUPS, "low": FOUR, "high": FOUR},
}
# One part beyond an exact exclusion each, as (the draft's key, the part, its excess path).
# Beside "signups excluding web" (4) each one changed the answer or could.
EXTRA = {
    "keeping, other dimension": (
        "where",
        {"field": CUSTOMER, "op": "=", "value": 102},  # 1
        "where[1]",
    ),
    "unreadable, other dimension": (
        "where",
        {"field": CUSTOMER, "op": ">", "value": 105},  # 1
        "where[1]",
    ),
    "keeping, excluded dimension": (
        "where",
        {"field": CHANNEL, "op": "=", "value": "store"},
        "where[1]",
    ),
    "dropping another value": (
        "where",
        {"field": CHANNEL, "op": "!=", "value": "partner"},
        "where[1]",
    ),
    "NOT IN the excluded value": (  # 3: drops the signup with no channel
        "where",
        {"field": CHANNEL, "op": "NOT IN", "value": ["web"]},
        "where[1]",
    ),
    "null-keeping drop of another value": ("where", _drops("partner")[0], "where[1]"),
    "child group": ("where", NO_STORE_B, "where[1]"),  # 2
    "metric filter": (  # no rows
        "metric_filters",
        {"expression": SIGNUPS, "op": "<", "value": 4},
        "metric_filters",
    ),
    "window the question doesn't state": (
        "time",
        {"temporal_role": SIGNED_UP, "start": "2024-01-01"},
        "time.start",
    ),
    "another clock": (
        "time",
        {"temporal_role": "temporal_role.shop_order_ordered_at"},
        "time.temporal_role",
    ),
    "dense rows": ("time", {"fill": True}, "time.fill"),
    "another calendar": ("time", {"calendar_id": "fiscal"}, "time.calendar_id"),
    "case": (  # 0
        "select",
        {
            "kind": "case",
            "whens": [
                {
                    "when": {"kind": "comparison", "op": ">", "left": SIGNUPS, "right": FOUR},
                    "then": SIGNUPS,
                }
            ],
            "else": {"kind": "literal", "value": 0},
        },
        "select[0]",
    ),
    "other aggregation": ("select", {**SIGNUPS, "aggregation": "count"}, "select[0]"),
    # Plain references to subjects the question doesn't name: 5, and a conversion rate.
    "other measure": ("select", {"measure": "measure.shop.order_count"}, "select[0]"),
    "other metric": ("select", {"metric": "metric.shop.signup_to_order_7d"}, "select[0]"),
    **{
        f"{kind} expression": ("select", {"kind": kind, **parts}, "select[0]")
        for kind, parts in KINDS.items()
    },
    "limit 0": ("limit", 0, "limit"),  # no rows
    "limit 1": ("limit", 1, "limit"),
    "limits": ("limits", {"max_rows": 1}, "limits"),
    "temporal role overrides": (
        "temporal_role_overrides",
        {SIGNUPS["measure"]: SIGNED_UP},
        "temporal_role_overrides",
    ),
    "observation scope": ("observation_scope", "query", "observation_scope"),
    "export": ("export", True, "export"),
    "route decisions": (
        "route_decisions",
        [
            {
                "source_entity": "entity.shop_customer",
                "target_entity": "entity.shop_order",
                "relationship_path": ["relationship.shop_order_customer"],
            }
        ],
        "route_decisions",
    ),
    "unknown key": ("rows_per_group", 1, "rows_per_group"),
}
# Rows whose draft fails Query IR validation on this package for some question or source,
# before the exclusion check is reported.
REFUSED = {
    *("another clock", "dense rows", "another calendar", "other aggregation", "other measure"),
    *(f"{kind} expression" for kind in ("aggregate_if", "prior_period", "rolling")),
    *(f"{kind} expression" for kind in ("cumulative", "period_to_date", "conversion")),
    *("distribution expression", "route decisions", "unknown key"),
}
VALIDATION_FAILED = {"VALIDATION_FAILED", "PLAN_FALLBACK_SEMANTIC_DRIFT"}


def _with(query: dict[str, Any], key: str, part: Any) -> dict[str, Any]:
    """``query`` with ``part``: merged into ``time``, in place of the selected expression,
    appended to a list, or set."""

    if key == "time":
        return {**query, "time": {**query.get("time", {}), **part}}
    if key == "select":
        return {**query, "select": [{**query["select"][0], "expression": part}]}
    if key in {"where", "metric_filters"}:
        return {**query, key: [*query.get(key, []), part]}
    return {**query, key: part}


def _caller(key: str, part: Any, added: dict[str, Any]) -> Any:
    """The caller's ``partial_query`` value at ``key`` that the planner merges into ``added``."""

    if key == "select":
        return [{"as": "caller_signups", "expression": part}]
    if key in {"where", "metric_filters"}:
        return [part]
    return added[key]


@pytest.mark.parametrize("source", ["generator", "caller"])
@pytest.mark.parametrize("extra", EXTRA)
@pytest.mark.parametrize(
    "question", ["signups excluding web", "signups excluding web in June 2024"]
)
def test_any_other_part_beside_an_exclusion_holds(shop, monkeypatch, question, extra, source):
    window = f" AND {JUNE_2024}" if "June" in question else ""
    exact = _plan(shop, question)
    _assert_ready(shop, exact, _reference(shop, "channel IS DISTINCT FROM 'web'" + window))
    query = exact["best"]["query_ir"]
    key, part, path = EXTRA[extra]
    added = _with(query, key, part)
    [gap] = _gaps(shop, question, added)
    assert path in [row["path"] for row in gap.actual["excess"]], gap.actual
    if source == "generator":
        _draft_plan(monkeypatch, added)
        payload = _plan(shop, question)
    else:
        partial = {"policy_context": NOW, key: _caller(key, part, added)}
        payload = plan_payload(shop, intent=question, partial_query=partial)
    if payload["why"]["code"] in VALIDATION_FAILED:
        assert extra in REFUSED, payload["why"]
        assert payload["status"] != "ok"
        assert "execute" not in payload["next"].get("ready_for", [])
    else:
        _assert_held(payload)


@pytest.mark.parametrize(
    "control",
    [
        lambda query: query,
        lambda query: {**query, "order_by": [{"field": "signup_count", "direction": "DESC"}]},
        lambda query: {**query, "_note": "drafted for a test"},
        lambda query: {**query, "verbosity": "minimal"},
        lambda query: _with(query, "select", {"kind": "measure", **SIGNUPS}),
    ],
    ids=["canonical", "order by the alias", "annotation", "verbosity", "kind measure"],
)
@pytest.mark.parametrize(
    "question", ["signups excluding web", "signups excluding web in June 2024"]
)
def test_what_changes_no_number_beside_an_exclusion_stays_ready(
    shop, monkeypatch, question, control
):
    window = f" AND {JUNE_2024}" if "June" in question else ""
    expected = _reference(shop, "channel IS DISTINCT FROM 'web'" + window)
    _draft_plan(monkeypatch, control(_plan(shop, question)["best"]["query_ir"]))
    _assert_ready(shop, _plan(shop, question), expected)


def test_every_query_ir_key_and_expression_kind_is_classified():
    # A new key or kind is excess beside an exclusion until it is admitted or has a row here.
    schema = json.loads((SHOP.parents[3] / "schemas" / "query_ir.v1.json").read_text())
    defs = schema["$defs"]
    rows = list(EXTRA.values())
    admitted = {*_INERT_KEYS, "group_by", "select", "where", "time"}
    assert set(schema["properties"]) <= admitted | {key for key, _part, _path in rows}
    shapes = [defs[ref["$ref"].rsplit("/", 1)[-1]] for ref in defs["SelectExpression"]["oneOf"]]
    shapes += [
        defs[ref["$ref"].rsplit("/", 1)[-1]] for shape in shapes for ref in shape.get("oneOf", [])
    ]
    kinds = {
        kind
        for shape in shapes
        for row in [shape.get("properties", {}).get("kind", {})]
        for kind in row.get("enum", [row["const"]] if "const" in row else [])
    }
    selected = {part.get("kind") for key, part, _path in rows if key == "select"}
    assert kinds - {"measure", "metric"} <= selected
    time = set(defs["TimeBlock"]["properties"]) - {
        "temporal_role",
        "grain",
        *("start", "end", "range"),
    }
    assert {f"time.{key}" for key in time} <= {path for key, _part, path in rows if key == "time"}


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
    ("question", "excluded"),
    [
        ("revenue excluding Brooklyn", "Brooklyn"),
        ("revenue not Brooklyn by month", "Brooklyn"),
        ("revenue for all stores except New Orleans", "New Orleans"),
    ],
)
def test_planned_store_exclusions_match_reference_sql(runtime_factory, question, excluded):
    # These held with a reversed '=' draft; the planner now drafts the null-keeping exclusion,
    # which keeps orders whose store has no name.
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=question)
        assert payload["status"] == "ok", payload.get("why")
        assert payload["next"]["ready_for"] == ["execute"]
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
