"""Authored questions are answered by their certified Query IR, never a fuzzy match."""

from pathlib import Path

import duckdb
import pytest
import yaml

from semantic_rails.planner import plan_payload
from semantic_rails.runtime import Runtime

NOW = {"now": "2026-10-05T06:00:00Z"}
QUESTION = "Which 2 accounts pay the most MRR today?"
ROLE = "temporal_role.subscriptions_account_day_day"
NAME = "dimension.subscriptions_account_name"
DAY = "dimension.subscriptions_account_day_day"
QUERY = {
    "version": 1,
    "select": [{"expression": {"metric": "metric.subscriptions.mrr"}, "as": "mrr"}],
    "group_by": [DAY, NAME],
    "time": {"temporal_role": ROLE, "grain": "day", "range": {"last": {"unit": "day", "value": 1}}},
    "order_by": [{"field": "mrr", "direction": "DESC"}, {"field": NAME, "direction": "ASC"}],
    "limit": 2,
}


def _write_examples(root: Path, entries: dict) -> None:
    path = root / "examples" / "core.yml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump({"examples": entries}), encoding="utf-8")


@pytest.fixture()
def subscriptions(tmp_path):
    root = tmp_path / "subscriptions"
    root.mkdir()
    files = {
        "package.yml": """
schema_version: 1
package:
  id: subscriptions
  namespace: subscriptions
  name: Subscriptions
  warehouse: duckdb
  default_db: subscriptions.duckdb
  seed: {kind: external}
defaults:
  time: {timezone: UTC}
""",
        "graph.yml": """
graph:
  entities:
    account: {key: [account_id], model: accounts}
    account_day: {key: [account_id, day], model: account_day}
""",
        "models/accounts.yml": """
model:
  id: accounts
  relation: accounts
  entities: {account: {}}
  dimensions:
    name: {kind: categorical}
    segment: {kind: categorical, domain: [customer, internal]}
""",
        "models/account_day.yml": """
model:
  id: account_day
  relation: account_day
  entities: {account_day: {}, account: {join: account_id}}
  times:
    day:
      column: day
      kind: date
      class: as_of_time
      default: true
      supported_grains: [day]
  dimensions:
    plan: {kind: categorical, domain: [basic, pro]}
  measures:
    mrr_all:
      expr: mrr
      accumulation: {kind: stock, snapshot: end_of_period}
      publish: false
""",
        "metrics/mrr.yml": """
metrics:
  mrr:
    label: MRR
    kind: semi_additive
    temporal_role: temporal_role.subscriptions_account_day_day
    expression:
      kind: semi_additive
      measure: measure.subscriptions.mrr_all
      filter:
        all: [{field: dimension.subscriptions_account_segment, op: '=', value: customer}]
""",
        "policies/snapshot.yml": """
policies:
  day_required:
    kind: metric_constraint
    object_ids: [measure.subscriptions.mrr_all]
    config:
      required_group_by: [dimension.subscriptions_account_day_day]
""",
    }
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    _write_examples(root, {"top_mrr": {"question": QUESTION, "query": QUERY}})
    with duckdb.connect(str(root / "subscriptions.duckdb")) as conn:
        conn.execute("""
CREATE TABLE accounts (account_id VARCHAR, name VARCHAR, segment VARCHAR);
INSERT INTO accounts VALUES ('a','Acme Data Co','customer'),('b','Globex','customer'),
  ('c','QA Sandbox','internal'),('d','Initech','customer');
CREATE TABLE account_day AS SELECT a.account_id, d::DATE AS day,
  CASE WHEN a.account_id='a' AND d >= DATE '2026-10-02' THEN 'pro' ELSE 'basic' END AS plan,
  CASE WHEN a.account_id='b' AND d >= DATE '2026-10-01' THEN 0
       WHEN a.account_id='a' AND d >= DATE '2026-10-02' THEN 500 ELSE 99 END::DOUBLE AS mrr
  FROM accounts a, range(DATE '2026-09-01', DATE '2026-10-05', INTERVAL 1 DAY) t(d);
""")
    runtime = Runtime.from_path(str(root))
    try:
        yield runtime
    finally:
        runtime.close()


def _plan(runtime, question=QUESTION, **kwargs):
    return plan_payload(runtime, intent=question, partial_query={"policy_context": NOW}, **kwargs)


def _reference(runtime, day, limit):
    with duckdb.connect(runtime.db_path, read_only=True) as conn:
        return conn.execute(
            "SELECT a.name, d.mrr FROM account_day d JOIN accounts a USING(account_id) "
            "WHERE a.segment = 'customer' AND d.day = ? ORDER BY d.mrr DESC, a.name LIMIT ?",
            [day, limit],
        ).fetchall()


def test_authored_snapshot_question_answers_reference(subscriptions):
    result = _plan(subscriptions)
    assert result["status"] == "ok", result.get("why")
    assert result["best"]["pattern"] == "package_example"
    rows = subscriptions.query(result["best"]["query_ir"])["rows"]
    gold = _reference(subscriptions, "2026-10-04", 2)
    assert gold == [("Acme Data Co", 500), ("Initech", 99)]
    assert [(row[NAME], row["mrr"]) for row in rows] == gold


@pytest.mark.parametrize(
    "question", [QUESTION + " ", QUESTION.upper(), QUESTION.replace(" ", "  ")]
)
def test_case_and_whitespace_variants_match_reference(subscriptions, question):
    result = _plan(subscriptions, question)
    assert result["status"] == "ok", result.get("why")
    assert result["best"]["pattern"] == "package_example"
    query = result["best"]["query_ir"]
    assert query["limit"] == 2
    assert query["time"]["start"] == "2026-10-04"
    assert query["time"]["end"] == "2026-10-05"
    rows = subscriptions.query(query)["rows"]
    assert [(row[NAME], row["mrr"]) for row in rows] == _reference(subscriptions, "2026-10-04", 2)
    assert result["best"]["interpreted_intent"]["consumed_spans"] == [[0, len(result["intent"])]]


def _replace_entries(runtime, entries):
    _write_examples(Path(runtime.package_root), entries)
    runtime.reload()


@pytest.mark.parametrize(
    "question", ["What's the top MRR?", "What’s the top MRR? ", "What is the top MRR?"]
)
def test_contractions_use_the_same_normalizer(subscriptions, question):
    _replace_entries(
        subscriptions, {"top_mrr": {"question": "What is the top MRR?", "query": QUERY}}
    )
    result = _plan(subscriptions, question)
    assert result["status"] == "ok", result.get("why")
    rows = subscriptions.query(result["best"]["query_ir"])["rows"]
    assert [(row[NAME], row["mrr"]) for row in rows] == _reference(subscriptions, "2026-10-04", 2)


@pytest.mark.parametrize(
    "question",
    [
        QUESTION.replace("most", "least"),
        QUESTION.replace("today", "today and yesterday"),
        QUESTION.replace("MRR", "ARR"),
        QUESTION.replace("today", "today for Acme"),
        QUESTION.replace("2", "2.5"),
        QUESTION.replace("2", "0"),
        "‘Which 2 accounts pay the most MRR today?’ ",
        "WHICH 2 ACCOUNT PAY THE MOST MRR TODAY",
        QUESTION.replace("2", "3"),
    ],
)
def test_other_differences_have_no_example_effect(subscriptions, question):
    result = _plan(subscriptions, question)
    assert (result.get("best") or {}).get("pattern") != "package_example"


@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
def test_two_matching_examples_clarify_instead_of_choosing(subscriptions, detail):
    _replace_entries(
        subscriptions,
        {
            "first": {"question": QUESTION, "query": QUERY},
            "second": {"question": QUESTION, "query": {**QUERY, "limit": 3}},
        },
    )
    result = _plan(subscriptions, detail=detail)
    assert result["status"] == "needs_clarification"
    assert result["best"] is None
    assert result["why"]["code"] == "PLAN_AMBIGUOUS_EXAMPLE"
    assert result["why"]["details"]["example_ids"] == ["first", "second"]


@pytest.mark.parametrize(
    "bad", [{"version": 2}, {"select": [{"expression": {"metric": "metric.missing"}}]}]
)
def test_invalid_examples_fall_through_to_normal_planning(subscriptions, bad):
    _replace_entries(
        subscriptions, {"invalid_mrr": {"question": QUESTION, "query": {**QUERY, **bad}}}
    )
    result = _plan(subscriptions)
    assert (result.get("best") or {}).get("pattern") != "package_example"
    assert result["why"]["details"]["invalid_examples"] == ["invalid_mrr"]


@pytest.mark.parametrize(
    "hidden", [NAME, "metric.subscriptions.mrr", "measure.subscriptions.mrr_all"]
)
def test_hidden_example_fails_like_an_absent_one_even_alongside_visible_match(
    subscriptions, hidden
):
    from dataclasses import replace

    from semantic_rails.schema import SemanticPolicyConfig

    # The visible query only uses the day dimension and stock metric. Hiding a leaf
    # also hides the metric; in that case no example may mention it in diagnostics.
    visible = {**QUERY, "group_by": [DAY], "order_by": [{"field": "mrr", "direction": "DESC"}]}
    _replace_entries(
        subscriptions,
        {
            "private_definition": {"question": QUESTION, "query": QUERY},
            "public_definition": {"question": QUESTION, "query": visible},
        },
    )
    subscriptions._config = replace(
        subscriptions._config,
        semantic_policies=[
            *subscriptions._config.semantic_policies,
            SemanticPolicyConfig(
                id="policy.hidden", kind="object_visibility", object_ids=[hidden], action="hidden"
            ),
        ],
    )
    result = _plan(subscriptions)
    assert hidden not in str(result)
    if hidden == NAME:
        assert "private_definition" not in str(result)
        assert result["status"] == "ok", result.get("why")
        assert result["best"]["pattern"] == "package_example"
    else:
        # Both read the hidden metric: each fails as it would without the metric.
        assert (result.get("best") or {}).get("pattern") != "package_example"
        assert result["why"]["details"]["invalid_examples"] == [
            "private_definition",
            "public_definition",
        ]


def test_examples_load_once_per_generation_and_have_no_source_fallback(subscriptions, monkeypatch):
    from semantic_rails import yaml_loader

    calls = []
    original = yaml_loader.load_named_entries

    def load(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(yaml_loader, "load_named_entries", load)
    assert _plan(subscriptions)["status"] == "ok"
    _write_examples(Path(subscriptions.package_root), {})
    assert _plan(subscriptions)["status"] == "ok"
    assert len(calls) == 1
    subscriptions.reload()
    assert (_plan(subscriptions).get("best") or {}).get("pattern") != "package_example"
    assert len(calls) == 2
    subscriptions.source_path = ""
    subscriptions._package_examples = None
    assert subscriptions._get_package_examples() == []
    assert len(calls) == 2


def test_caller_fields_survive_and_resolved_ids_come_from_the_query(subscriptions):
    result = plan_payload(
        subscriptions,
        intent=QUESTION,
        partial_query={
            "policy_context": NOW,
            "limit": 3,
            "where": [{"field": NAME, "op": "=", "value": "Acme Data Co"}],
        },
    )
    assert result["status"] == "ok", result.get("why")
    query = result["best"]["query_ir"]
    assert query["limit"] == 3
    assert query["where"][0]["value"] == "Acme Data Co"
    assert "policy_context" not in query
    assert {row["id"] for row in result["best"]["resolved"]} == {
        DAY,
        NAME,
        ROLE,
        "metric.subscriptions.mrr",
    }
    rows = subscriptions.query(query)["rows"]
    assert [(row[NAME], row["mrr"]) for row in rows] == [("Acme Data Co", 500)]


@pytest.mark.parametrize("limit", [1, 3])
def test_another_count_has_no_example_effect_whatever_the_limit(subscriptions, limit):
    _replace_entries(
        subscriptions, {"top_mrr": {"question": QUESTION, "query": {**QUERY, "limit": limit}}}
    )
    result = _plan(subscriptions, QUESTION.replace("2", "4"))
    assert (result.get("best") or {}).get("pattern") != "package_example"


@pytest.mark.parametrize("day,limit", [("2026-09-30", 2), ("2026-09-30", 3), ("2026-10-02", 2)])
def test_another_day_than_the_authored_one_has_no_example_effect(subscriptions, day, limit):
    # Yesterday agrees with the query, but an example answers only its own day.
    _replace_entries(
        subscriptions,
        {"top_mrr": {"question": QUESTION.replace("today", "yesterday"), "query": QUERY}},
    )
    question = QUESTION.replace("today", f"on {day}").replace("2 accounts", f"{limit} accounts")
    result = _plan(subscriptions, question)
    assert (result.get("best") or {}).get("pattern") != "package_example"


def _jaffle_day_revenue(day: str, end: str) -> dict:
    return {
        "version": 1,
        "select": [
            {
                "expression": {"measure": "measure.jaffle.revenue_usd", "aggregation": "sum"},
                "as": "revenue_usd",
            }
        ],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "day",
            "start": day,
            "end": end,
        },
        "where": [
            {"field": "dimension.jaffle_order_ordered_at", "op": ">=", "value": day},
            {"field": "dimension.jaffle_order_ordered_at", "op": "<", "value": end},
        ],
    }


@pytest.mark.parametrize(
    "day,end,other_day",
    [
        ("2026-09-30", "2026-10-01", "2026-09-29"),
        # The fixture's orders end in 2017: this day has rows to sum.
        ("2017-08-30", "2017-08-31", "2017-08-29"),
    ],
)
def test_an_example_answers_only_its_own_day(runtime_factory, day, end, other_day):
    runtime = runtime_factory("jaffle_shop")
    try:
        _replace_entries(
            runtime,
            {
                "day_revenue": {
                    "question": f"Revenue on {day}",
                    "query": _jaffle_day_revenue(day, end),
                }
            },
        )
        other = plan_payload(runtime, intent=f"Revenue on {other_day}")
        verbatim = plan_payload(runtime, intent=f"Revenue on {day}")
        assert verbatim["status"] == "ok", verbatim.get("why")
        assert verbatim["best"]["pattern"] == "package_example"
        rows = runtime.query(verbatim["best"]["query_ir"])["rows"]
        with duckdb.connect(runtime.db_path, read_only=True) as conn:
            (gold,) = conn.execute(
                "SELECT SUM(order_total_cents / 100.0) FROM jaffle_order "
                "WHERE ordered_at >= CAST(? AS DATE) AND ordered_at < CAST(? AS DATE)",
                [day, end],
            ).fetchone()
    finally:
        runtime.close()
    assert (other.get("best") or {}).get("pattern") != "package_example"
    actual = [row["revenue_usd"] for row in rows]
    if gold is None:
        assert actual in ([], [None])
    else:
        assert len(actual) == 1
        assert float(actual[0]) == pytest.approx(float(gold))


def _jaffle_revenue_above(value: float) -> dict:
    total = {"measure": "measure.jaffle.revenue_usd", "aggregation": "sum"}
    return {
        "version": 1,
        "select": [{"expression": total, "as": "revenue_usd"}],
        "metric_filters": [{"expression": total, "op": ">", "value": value}],
    }


def test_a_signed_currency_threshold_answers_only_its_own_sign(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        runtime._package_examples = [
            (
                "negative",
                {"question": "Total revenue above -$5", "query": _jaffle_revenue_above(-5)},
            )
        ]
        verbatim = plan_payload(runtime, intent="Total revenue above -$5")
        unsigned = plan_payload(runtime, intent="Total revenue above $5")
        assert verbatim["status"] == "ok", verbatim.get("why")
        assert verbatim["best"]["pattern"] == "package_example"
        rows = runtime.query(verbatim["best"]["query_ir"])["rows"]
        with duckdb.connect(runtime.db_path, read_only=True) as conn:
            gold = conn.execute(
                "SELECT SUM(order_total_cents / 100.0) FROM jaffle_order "
                "HAVING SUM(order_total_cents / 100.0) > -5"
            ).fetchall()
        runtime._package_examples = [
            ("positive", {"question": "Total revenue above $5", "query": _jaffle_revenue_above(5)})
        ]
        signed = plan_payload(runtime, intent="Total revenue above -$5")
    finally:
        runtime.close()
    assert len(gold) == 1
    assert [float(row["revenue_usd"]) for row in rows] == pytest.approx([float(gold[0][0])])
    assert (unsigned.get("best") or {}).get("pattern") != "package_example"
    assert (signed.get("best") or {}).get("pattern") != "package_example"


@pytest.mark.parametrize("time_phrase", ["on 2026-09-30", "yesterday"])
def test_another_window_never_reuses_the_authored_one(subscriptions, time_phrase):
    result = _plan(subscriptions, QUESTION.replace("today", time_phrase))
    assert (result.get("best") or {}).get("pattern") != "package_example"


def _bundled_examples():
    from semantic_rails.yaml_loader import load_named_entries

    return load_named_entries(
        Path("configs/semantic_rails/jaffle_shop/examples"),
        plural_key="examples",
        singular_key="example",
    )


@pytest.mark.parametrize(
    "example_id,entry",
    _bundled_examples(),
    ids=lambda item: item if isinstance(item, str) else None,
)
def test_bundled_examples_verbatim_equal_authored_results(runtime_factory, example_id, entry):
    runtime = runtime_factory("jaffle_shop")
    result = plan_payload(runtime, intent=entry["question"])
    assert result["status"] == "ok", (example_id, result.get("why"))
    assert result["best"]["pattern"] == "package_example"
    assert result["best"]["query_ir"] == entry["query"]
    actual = runtime.query(result["best"]["query_ir"])
    gold = runtime.query(entry["query"])
    assert actual["output_columns"] == gold["output_columns"]
    assert actual["rows"] == gold["rows"]
    runtime.close()


@pytest.mark.parametrize("question", ["Revenue above 2 dollars", "Revenue above 3 dollars"])
def test_threshold_equal_to_limit_is_not_a_count(subscriptions, question):
    _replace_entries(
        subscriptions, {"threshold": {"question": "Revenue above 2 dollars", "query": QUERY}}
    )
    result = _plan(subscriptions, question)
    if "3" in question:
        assert (result.get("best") or {}).get("pattern") != "package_example"
    else:
        assert result["status"] == "ok"


@pytest.mark.parametrize("time", ["invalid", 2])
def test_invalid_example_time_shape_does_not_crash_matching(subscriptions, time):
    _replace_entries(
        subscriptions, {"bad_time": {"question": QUESTION, "query": {**QUERY, "time": time}}}
    )
    result = _plan(subscriptions, QUESTION.replace("2", "3"))
    assert (result.get("best") or {}).get("pattern") != "package_example"


@pytest.mark.parametrize("phrase", ["last 7 days", "this week", "this year", "last month", "today"])
def test_another_time_phrase_has_no_example_effect(subscriptions, phrase):
    _replace_entries(
        subscriptions,
        {"top_mrr": {"question": QUESTION.replace("today", "yesterday"), "query": QUERY}},
    )
    result = _plan(subscriptions, QUESTION.replace("today", phrase))
    assert (result.get("best") or {}).get("pattern") != "package_example"


def test_a_longer_window_has_no_example_effect(subscriptions):
    week = {**QUERY, "time": {**QUERY["time"], "range": {"last": {"unit": "day", "value": 7}}}}
    _replace_entries(
        subscriptions,
        {"top_mrr": {"question": QUESTION.replace("today", "last 7 days"), "query": week}},
    )
    result = _plan(subscriptions, QUESTION.replace("today", "last 14 days"))
    assert (result.get("best") or {}).get("pattern") != "package_example"


_AUGUST = {"temporal_role": ROLE, "grain": "month", "start": "2026-08-01", "end": "2026-09-01"}
_LAST_MONTH = {
    "temporal_role": ROLE,
    "grain": "month",
    "range": {"last": {"unit": "month", "value": 1}},
}


@pytest.mark.parametrize(
    "time,authored,asked",
    [
        (_AUGUST, "August 2026", "July 2026"),
        (_AUGUST, "August 2026", "last month"),
        ({**_LAST_MONTH, "fill": True}, "last month", "July 2026"),
        (_LAST_MONTH, "last month", "2026-07-15"),
        (_LAST_MONTH, "last month", "Q3 2026"),
        (_LAST_MONTH, "last month", "last 2 months"),
        (_LAST_MONTH, "last month", "this month"),
        ({**_AUGUST, "calendar_id": "fiscal"}, "August 2026", "July 2026"),
        (
            {**_LAST_MONTH, "range": {"last": {"unit": "month", "value": 2}}},
            "last 2 months",
            "last 3 months",
        ),
    ],
)
def test_the_authored_time_block_is_never_edited(time, authored, asked):
    from semantic_rails.planner.examples import _match

    query = {**QUERY, "time": time}
    assert _match(f"MRR by account {asked}", f"MRR by account {authored}", query) is None
    assert _match(f"MRR by account {authored}", f"MRR by account {authored}", query) == query


@pytest.mark.parametrize(
    "authored,asked",
    [
        ("pay more than 2.5 MRR", "pay more than 25 MRR"),
        ("pay more than 25 MRR", "pay more than 2.5 MRR"),
        ("changed MRR by -5", "changed MRR by 5"),
        ("pay 1-5 MRR", "pay 15 MRR"),
        ("pay more than .5 MRR", "pay more than 5 MRR"),
        ("pay 9.0 MRR", "pay 90 MRR"),
    ],
)
def test_numbers_compare_whole(subscriptions, authored, asked):
    _replace_entries(
        subscriptions,
        {"threshold": {"question": f"Which 2 accounts {authored} yesterday?", "query": QUERY}},
    )
    verbatim = _plan(subscriptions, f"Which 2 accounts {authored} yesterday?")
    assert verbatim["best"]["pattern"] == "package_example"
    # One number is never read as another, whatever the count or day asked.
    for count, phrase in [("2", "yesterday"), ("3", "yesterday"), ("2", "on 2026-09-30")]:
        result = _plan(subscriptions, f"Which {count} accounts {asked} {phrase}?")
        assert (result.get("best") or {}).get("pattern") != "package_example"


@pytest.mark.parametrize(
    "authored,asked",
    [
        ("5%", "$5"),
        ("5%", "5"),
        ("$5", "5%"),
        ("$5", "5"),
        ("5", "5%"),
        ("$5", "€5"),
        ("-$5", "$5"),
        ("$5", "-$5"),
        ("-€5", "€5"),
        ("€5", "-€5"),
        ("- 5", "5"),
        ("5", "- 5"),
    ],
)
def test_symbols_keep_their_meaning(subscriptions, authored, asked):
    question = "Which orders have a discount above {}?"
    _replace_entries(
        subscriptions, {"discount": {"question": question.format(authored), "query": QUERY}}
    )
    verbatim = _plan(subscriptions, question.format(authored))
    assert verbatim["best"]["pattern"] == "package_example"
    result = _plan(subscriptions, question.format(asked))
    assert (result.get("best") or {}).get("pattern") != "package_example"


@pytest.mark.parametrize(
    "question",
    [
        QUESTION.replace("today", "yesterday").replace("2", "-2"),
        QUESTION.replace("today", "-2026-09-30"),
    ],
)
def test_a_signed_number_never_matches_its_unsigned_form(subscriptions, question):
    _replace_entries(
        subscriptions,
        {"top_mrr": {"question": QUESTION.replace("today", "yesterday"), "query": QUERY}},
    )
    result = _plan(subscriptions, question)
    assert (result.get("best") or {}).get("pattern") != "package_example"


def _values(node):
    if isinstance(node, dict):
        for key, child in node.items():
            if key == "value":
                yield child
            yield from _values(child)
    elif isinstance(node, list):
        for child in node:
            yield from _values(child)


def test_bundled_decimal_threshold_is_not_its_whole_number_neighbour(runtime_factory):
    entry = dict(_bundled_examples())["signup_to_send_28d_for_high_order_rate_stores"]
    assert "above 90 percent" in entry["question"]
    runtime = runtime_factory("jaffle_shop")
    try:
        result = plan_payload(
            runtime, intent=entry["question"].replace("above 90 percent", "above 9.0 percent")
        )
    finally:
        runtime.close()
    assert (result.get("best") or {}).get("pattern") != "package_example"
    assert 0.9 not in list(_values(result))


@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
def test_hidden_id_in_a_mapping_key_fails_the_example_like_an_absent_one(runtime_factory, detail):
    import json
    from dataclasses import replace

    from semantic_rails.schema import SemanticPolicyConfig

    hidden = "measure.jaffle.drink_revenue_usd"
    runtime = runtime_factory("jaffle_shop")
    try:
        _replace_entries(
            runtime,
            {
                "keyed_override": {
                    "question": "Total revenue",
                    "query": {
                        "version": 1,
                        "select": [
                            {
                                "expression": {
                                    "measure": "measure.jaffle.revenue_usd",
                                    "aggregation": "sum",
                                },
                                "as": "revenue_usd",
                            }
                        ],
                        "temporal_role_overrides": {hidden: "temporal_role.jaffle_order_time"},
                    },
                }
            },
        )
        visible = plan_payload(runtime, intent="Total revenue")
        assert visible["best"]["pattern"] == "package_example"
        runtime._config = replace(
            runtime._config,
            semantic_policies=[
                *runtime._config.semantic_policies,
                SemanticPolicyConfig(
                    id="policy.hidden",
                    kind="object_visibility",
                    object_ids=[hidden],
                    action="hidden",
                ),
            ],
        )
        payload = json.dumps(plan_payload(runtime, intent="Total revenue", detail=detail))
    finally:
        runtime.close()
    assert hidden not in payload
    result = json.loads(payload)
    assert (result.get("best") or {}).get("pattern") != "package_example"
    assert result["why"]["details"]["invalid_examples"] == ["keyed_override"]


def test_a_hidden_measure_answers_its_example_question_like_an_absent_one(tmp_path):
    import json
    from dataclasses import replace

    from semantic_rails.config import load_package_config
    from semantic_rails.schema import SemanticPolicyConfig
    from semantic_rails.visible_view import hidden_object_ids
    from tests.semantic_rails.conftest import copy_package_config
    from tests.semantic_rails.hidden_absent import absent

    hidden = "measure.jaffle.revenue_usd"
    root = copy_package_config(tmp_path, "jaffle_shop")
    config = load_package_config(str(root))
    policy = SemanticPolicyConfig(
        id="policy.hidden", kind="object_visibility", object_ids=[hidden], action="hidden"
    )
    governed = replace(config, semantic_policies=[*config.semantic_policies, policy])
    packages = {
        "hidden": governed,
        "absent": absent(config, hidden_object_ids(governed)),
    }
    answers = {}
    for name, package in packages.items():
        runtime = Runtime.from_config(package, source_path=str(root))
        try:
            answers[name] = plan_payload(runtime, intent="Monthly revenue")
        finally:
            runtime.close()

    def outcome(payload):
        why = payload.get("why") or {}
        return payload["status"], why.get("code"), why.get("details", {}).get("invalid_examples")

    assert outcome(answers["hidden"]) == outcome(answers["absent"])
    assert outcome(answers["absent"])[1:] == ("PLAN_INVALID_EXAMPLE", ["monthly_revenue"])
    assert hidden not in json.dumps(answers["hidden"])


def test_example_draft_cannot_bypass_shared_planned_row_validation(subscriptions, monkeypatch):
    from semantic_rails.planner import plan as plan_module

    original = plan_module._planned_row
    calls = []

    def refuse(*args):
        calls.append(args[2])
        row = original(*args)
        row["validation"] = {
            "ok": False,
            "errors": [{"code": "POLICY_DENIED", "message": "Held by shared planning validation."}],
        }
        return row

    monkeypatch.setattr(plan_module, "_planned_row", refuse)
    result = _plan(subscriptions)
    assert calls == ["package_example"]
    assert result["status"] == "low_confidence"
    assert result["best"]["validation_ok"] is False
    assert not result["next"]["ready_for"]
