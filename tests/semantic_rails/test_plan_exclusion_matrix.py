"""Every item an exclusion names needs its own exact predicate, and a time exclusion holds.

An exclusion keeps rows with no recorded value, so only ``IS DISTINCT FROM`` realizes it:
"signups excluding web" counts the store signups and the one with no channel. Ready plans are
compared with independent DuckDB SQL by the customers they select and by their total.
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
    return [
        (item.kind, item.text)
        for clause in exclusion_clauses(runtime._config, question, NOW)
        for item in clause.items
    ]


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
    gaps = exclusion_gaps(shop._config, question, {"where": _drops("web", "store")})
    assert gaps == [], question
    [gap] = exclusion_gaps(shop._config, question, {"where": _drops("web")})
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
        assert len(exclusion_gaps(runtime._config, question, {"where": _drops("web")})) == 1
        # The planner drafts the second item too.
        payload = _plan(runtime, question, _drops("web"))
        _assert_ready(runtime, payload, _reference(runtime, "channel IS NULL"))
        _draft_plan(monkeypatch, _signups(_drops("web")))
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
    gaps = exclusion_gaps(shop._config, question, {"where": where, "policy_context": NOW})
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
    # The caller's own exclusion of another value may stand beside the question's.
    _assert_ready(
        shop,
        _plan(shop, question, [*_drops("partner", op="!="), *_drops("web")]),
        _reference(shop, "channel <> 'partner' AND channel IS DISTINCT FROM 'web'"),
    )
    gaps = exclusion_gaps(shop._config, question, {"where": _drops("store")})
    assert [gap.actual["missing"] for gap in gaps] == [["web"]]
    assert exclusion_gaps(
        shop._config, question, {"where": _drops("store")}, caller={"where": _drops("store")}
    )[0].actual["missing"] == ["web"]
