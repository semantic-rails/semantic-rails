"""The REPL metric wizard, driven by prompt label through the public REPL entry point.

Tables cover recipe answers -> YAML, saved YAML -> Enter everywhere -> the same
metric, and changed inputs -> refreshed defaults, then filters, expressions the
recipes cannot write, both real prompt backends and undo. Metrics run on DuckDB.
"""

from __future__ import annotations

import itertools
import sys
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.cli import scaffold
from semantic_rails.config_validation import PackageReference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.repl import authoring, backend, shell


class _Script:
    """A prompt backend that answers by label and records the default each prompt offered.

    ``answers`` maps a prompt label to its answer; for a choice, the answer is a
    piece of the option's description. Unlisted prompts take their default.
    """

    name = "script"
    filters_long_lists = True

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.offered: dict[str, Any] = {}
        self.options: dict[str, list[str]] = {}
        self._asked: dict[str, int] = {}

    def _ask(self, label: str) -> Any:
        # A wizard that re-asks the same question forever fails the test instead of hanging.
        self._asked[label] = self._asked.get(label, 0) + 1
        assert self._asked[label] <= 5, f"asked {label!r} more than 5 times"
        return self.answers.get(label)

    def text(self, label: str, *, default: str = "") -> str:
        wanted = self._ask(label)
        self.offered[label] = default
        return default if wanted is None else str(wanted)

    def confirm(self, label: str, *, default: bool) -> bool:
        wanted = self._ask(label)
        self.offered[label] = default
        return default if wanted is None else bool(wanted)

    def choose(self, label: str, options: Sequence[tuple[str, str]], *, default: str = "") -> str:
        wanted = self._ask(label)
        described = dict(options)
        assert not default or default in described, f"{label!r} default {default!r} is not offered"
        self.offered[label] = described.get(default, "")
        self.options[label] = list(described.values())
        if wanted is None:
            return default
        return next(value for value, text in options if str(wanted) in text)

    def multi_choose(
        self, label: str, options: Sequence[tuple[str, str]], *, defaults: Collection[str] = ()
    ) -> list[str]:
        wanted = self._ask(label)
        self.offered[label] = list(defaults)
        self.options[label] = [text for _, text in options]
        if wanted is None:
            return list(defaults)
        return [value for value, text in options if value in wanted or text in wanted]

    def show_yaml(self, payload: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    yield
    backend.set_backend(None)


def _starter(tmp_path: Path) -> Path:
    """The scaffolded starter package: one events model with a default clock."""

    return Path(
        scaffold.create_project_report(
            package_id="shop", workspace_root=str(tmp_path), run_checks=False
        )["project_path"]
    )


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _authored(folder: Path) -> dict[str, bytes]:
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in folder.rglob("*.yml")
        if ".architect" not in path.parts
    }


def _repl(project: Path, line: str, script: _Script | None, undo: list[Any]) -> None:
    if script is not None:
        backend.set_backend(script)
    shell._handle_repl_line(line, PackageReference(source_path=str(project)), undo_stack=undo)


META = {"owner_team": "analytics", "review_priority": "medium", "change_risk": "low"}
REVENUE = "measure.shop.revenue"
ORDER_COUNT = "measure.shop.order_count"
ORDERED = "temporal_role.shop_order_ordered_at"
SHIPPED = "temporal_role.shop_order_shipped_at"
# How the selectors describe these rows.
REVENUE_ROW = "revenue - [measure] Revenue - orders"
ORDER_COUNT_ROW = "order_count - [measure] Order count - orders"
ABSENT = object()

MODELS = """
orders:
  label: Orders
  relation: raw_orders
  entities: {order: {}}
  times:
    ordered_at: {label: Ordered At, column: ordered_at, kind: timestamp, class: event_time, default: true}
  dimensions:
    status: {label: Status, kind: categorical, domain: [placed, completed, returned]}
    stage:
      label: Stage
      kind: categorical
      column: status
      domain: [{value: placed, label: New orders}, {value: completed, label: Completed orders},
               {value: returned, label: Returns}]
    channel: {label: Channel, kind: categorical}
    code: {label: Code, kind: categorical}
    is_priority: {label: Is priority, kind: boolean}
    tier: {label: Tier, kind: integer}
  measures:
    revenue: {label: Revenue, kind: aggregate, expr: subtotal, default_agg: sum,
              accumulation: {kind: flow}, value_type: currency, currency: USD}
    tax: {label: Tax, kind: aggregate, expr: tax_paid, default_agg: sum,
          accumulation: {kind: flow}, value_type: currency, currency: USD}
    order_count: {label: Order count, name: shop.OrdersPlaced, kind: entity_count,
                  entity_key: order_id, accumulation: {kind: event}, value_type: count}
customers:
  label: Customers
  relation: raw_customers
  entities: {customer: {}}
  measures:
    customer_count: {label: Customer count, kind: entity_count, entity_key: customer_id,
                     accumulation: {kind: event}, value_type: count}
calendar:
  relation: calendar
  calendar_id: default
  entities: {time: {}}
  times:
    date_day: {label: Calendar day, column: date_day, kind: date, class: calendar_time}
  dimensions:
    week_start: {label: Calendar week start, kind: date}
    month_start: {label: Calendar month start, kind: date}
    quarter_start: {label: Calendar quarter start, kind: date}
    year_start: {label: Calendar year start, kind: date}
"""
ENTITIES = """
order: {label: Order, key: [order_id], model: orders, allowed_as_root: true}
customer: {label: Customer, key: [customer_id], model: customers, allowed_as_root: true}
time: {label: Calendar, kind: time, key: [date_day], model: calendar, allowed_as_root: false}
"""
# order, day ordered, status, channel, code, subtotal, day shipped
ORDERS = [
    (1, 1, "completed", "web", "001", 10, 1),
    (2, 2, "returned", "store", "true", 20, 2),
    (3, 3, "placed", "phone", "1", 30, 3),
]
SHIPPED_LATE = [
    (4, 1, "completed", "store", "x", 30, 2),
    (5, 2, "returned", "web", "x", 40, 3),
    (6, 3, "placed", "web", "x", 50, 4),
]


def _shop(tmp_path: Path, *, calendar: bool = False, shipped: bool = False) -> Path:
    """The starter package plus orders and customers, on DuckDB.

    ``shipped`` adds a second order clock that revenue and the order count
    support, and three more orders that ship a day after they are placed.
    ``calendar`` adds the date spine that rolling windows, prior periods and
    growth need.
    """

    project = _starter(tmp_path)
    models, entities = yaml.safe_load(MODELS), yaml.safe_load(ENTITIES)
    orders = models["orders"]
    if shipped:
        clock = {**orders["times"]["ordered_at"], "column": "shipped_at", "default": False}
        orders["times"]["shipped_at"] = {**clock, "label": "Shipped At"}
        for measure in ("revenue", "order_count"):
            orders["measures"][measure]["times"] = ["ordered_at", "shipped_at"]
    if not calendar:
        del models["calendar"], entities["time"]
    for model_id, model in models.items():
        for measure in model.get("measures", {}).values():
            measure["meta"] = META
        _write_yaml(
            project / "models" / "core" / f"{model_id}.yml", {"model": {"id": model_id, **model}}
        )
    graph = yaml.safe_load((project / "graph.yml").read_text(encoding="utf-8"))
    graph["graph"]["entities"].update(entities)
    _write_yaml(project / "graph.yml", graph)

    rows = ", ".join(
        f"({order}, TIMESTAMP '2026-01-0{day}', '{status}', '{channel}', '{code}', {amount}.0, "
        f"TIMESTAMP '2026-01-0{ship}')"
        for order, day, status, channel, code, amount, ship in (
            ORDERS + SHIPPED_LATE if shipped else ORDERS
        )
    )
    with duckdb.connect(str(project / "data" / "shop.duckdb")) as conn:
        conn.execute(
            "CREATE TABLE raw_events AS SELECT 1 AS event_id, "
            "TIMESTAMP '2026-01-01' AS occurred_at, 'starter' AS event_type, 1.0 AS amount"
        )
        conn.execute(
            "CREATE TABLE raw_orders AS SELECT *, order_id IN (1, 3) AS is_priority, "
            "CASE WHEN order_id < 3 THEN 1 ELSE 2 END AS tier, 1.0 AS tax_paid FROM "
            f"(VALUES {rows}) AS t(order_id, ordered_at, status, channel, code, subtotal, shipped_at)"
        )
        conn.execute("CREATE TABLE raw_customers AS SELECT 1 AS customer_id")
        starts = ", ".join(
            f"date_trunc('{grain}', date_day)::DATE AS {grain}_start"
            for grain in ("week", "month", "quarter", "year")
        )
        conn.execute(
            f"CREATE TABLE calendar AS SELECT date_day, {starts} FROM "
            "generate_series(DATE '2026-01-01', DATE '2026-01-04', INTERVAL 1 DAY) AS d(date_day)"
        )
    return project


def _path(project: Path, key: str) -> Path:
    return project / "metrics" / "core" / f"{key}.yml"


def _saved(project: Path, key: str) -> dict[str, Any]:
    path = _path(project, key)
    return yaml.safe_load(path.read_text("utf-8"))["metrics"][key] if path.exists() else {}


def _write_metric(project: Path, key: str, spec: dict[str, Any]) -> Path:
    identity = {"as": f"metric.shop.{key}", "label": key.upper(), "description": "Authored."}
    _path(project, key).parent.mkdir(exist_ok=True)
    _write_yaml(_path(project, key), {"metrics": {key: {**identity, **spec, "meta": META}}})
    return _path(project, key)


def _author(
    project: Path, answers: dict[str, Any], undo: list[Any] | None = None
) -> tuple[_Script, dict[str, Any]]:
    """Run `author metric`, confirming the write; returns the script and the saved metric."""

    confirm = (
        "Manage and update this existing metric?",
        "Create this metric?",
        "Update this metric?",
    )
    script = _Script({**dict.fromkeys(confirm, True), **answers})
    _repl(project, "author metric", script, [] if undo is None else undo)
    return script, _saved(project, answers["Metric key"])


def _values(project: Path, key: str, clock: str | None = ORDERED) -> list[Any]:
    """The metric on ``clock`` by day, or by its window's unit; its total without a clock."""

    from semantic_rails.runtime import Runtime

    units = ["day", "week", "month", "quarter", "year"]
    text = _path(project, key).read_text(encoding="utf-8")
    grain = max((unit for unit in units if f"unit: {unit}" in text), key=units.index, default="day")
    query: dict[str, Any] = {
        "version": 1,
        "select": [{"as": "v", "expression": {"metric": f"metric.shop.{key}"}}],
    }
    if clock:
        query["time"] = {"temporal_role": clock, "grain": grain}
    runtime = Runtime.from_path(str(project))
    try:
        rows = runtime.query(query)["rows"]
    finally:
        runtime.close()
    rows.sort(key=lambda row: [str(value) for name, value in sorted(row.items()) if name != "v"])
    return [row["v"] for row in rows]


def _assert_has(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    for name, value in expected.items():
        if value is ABSENT:
            assert name not in actual, name
        else:
            assert actual.get(name) == value, (name, actual.get(name))


NEW_TAX_MEASURE = {
    "Metric key": "tax_collected",
    "Measure to publish": "Create a new measure first",
    "Model to extend": "events - ",
    "Measure key": "tax",
    "Column or scalar expression (for example amount_cents / 100.0)": "tax_paid",
    "Result type": "Currency",
    "Create this measure?": True,
    "Create this metric?": True,
}
TAX_METRIC = "metrics/core/tax_collected.yml"
EVENTS = "models/core/events.yml"


@pytest.mark.parametrize("edited", [None, EVENTS, TAX_METRIC])
def test_one_undo_takes_back_an_inline_measure_and_its_metric_or_neither(
    tmp_path: Path, edited: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _starter(tmp_path)
    before = _authored(project)
    undo: list[Any] = []
    _repl(project, "author metric", _Script(NEW_TAX_MEASURE), undo)

    events = yaml.safe_load((project / EVENTS).read_text("utf-8"))["model"]
    metric = yaml.safe_load((project / TAX_METRIC).read_text("utf-8"))["metrics"]
    assert events["measures"]["tax"]["expr"] == "tax_paid"
    assert metric["tax_collected"]["measure"] == "measure.shop.tax"
    assert len(undo) == 1 and undo[0].report["parse"]["ok"] is True

    if edited:
        # Undo checks both files before restoring either one.
        authored = (project / edited).read_bytes()
        (project / edited).write_bytes(authored + b"\n# external edit\n")
        after_edit = _authored(project)
        _repl(project, "undo", None, undo)
        assert "Undo was not applied" in capsys.readouterr().out
        assert len(undo) == 1 and _authored(project) == after_edit
        (project / edited).write_bytes(authored)

    _repl(project, "undo", None, undo)
    assert undo == [] and _authored(project) == before


@pytest.mark.parametrize("edit_before_cancel", [False, True])
def test_cancelling_the_metric_takes_back_its_inline_measure_unless_edited_since(
    tmp_path: Path, edit_before_cancel: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _starter(tmp_path)
    before = _authored(project)

    class _EditThenCancel(_Script):
        def confirm(self, label: str, *, default: bool) -> bool:
            if label == "Create this metric?" and edit_before_cancel:
                (project / EVENTS).write_bytes((project / EVENTS).read_bytes() + b"# edit\n")
            return super().confirm(label, default=default)

    script = _EditThenCancel({**NEW_TAX_MEASURE, "Create this metric?": False})
    undo: list[Any] = []
    _repl(project, "author metric", script, undo)

    output = capsys.readouterr().out
    assert not (project / TAX_METRIC).exists()
    if edit_before_cancel:
        # Nothing is restored, the kept measure is named, and `undo` can still reach it.
        assert "no files changed" not in output
        assert f"conflict {project / EVENTS}" in output and "kept    measure `tax`" in output
        assert len(undo) == 1
        edited = (project / EVENTS).read_bytes()
        (project / EVENTS).write_bytes(edited.removesuffix(b"# edit\n"))
        _repl(project, "undo", None, undo)
    else:
        assert "Authoring cancelled; no files changed." in output
    assert undo == [] and _authored(project) == before


@pytest.mark.parametrize("create_metric", [True, False])
def test_an_unchanged_inline_measure_does_not_block_undo_or_cancel(
    tmp_path: Path, create_metric: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _starter(tmp_path)
    _repl(project, "author measure", _Script(NEW_TAX_MEASURE), [])
    before_metric = _authored(project)
    undo: list[Any] = []
    manage_tax = {
        **NEW_TAX_MEASURE,
        "Manage and update this existing measure?": True,
        "Column or scalar expression (for example amount_cents / 100.0)": None,
        "Update this measure?": True,
        "Create this metric?": create_metric,
    }

    _repl(project, "author metric", _Script(manage_tax), undo)

    if create_metric:
        assert [part._snapshots == () for part in undo[0].parts] == [True, False]
        _repl(project, "undo", None, undo)
    else:
        assert "Authoring cancelled; no files changed." in capsys.readouterr().out
    assert undo == [] and _authored(project) == before_metric


@pytest.mark.parametrize("create_metric", [True, False])
@pytest.mark.parametrize("external_edit", [False, True])
def test_two_inline_measures_keep_an_edit_made_between_them(
    tmp_path: Path, external_edit: bool, create_metric: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _starter(tmp_path)
    before = _authored(project)

    class _TwoMeasures(_Script):
        keys = iter(["tax", "shipping"])
        columns = iter(["tax_paid", "shipping_paid"])

        def text(self, label: str, *, default: str = "") -> str:
            if label == "Measure key":
                return next(self.keys)
            if label.startswith("Column or scalar expression"):
                return next(self.columns)
            return super().text(label, default=default)

        def confirm(self, label: str, *, default: bool) -> bool:
            if label == "Create this measure?" and self._asked.get(label) == 1 and external_edit:
                model = yaml.safe_load((project / EVENTS).read_text("utf-8"))
                model["model"]["label"] = "Externally labeled events"
                _write_yaml(project / EVENTS, model)
            return super().confirm(label, default=default)

    script = _TwoMeasures(
        {
            "Metric key": "tax_to_shipping",
            "Metric recipe": "Ratio",
            "Numerator": "Create a new measure first",
            "Denominator": "Create a new measure first",
            "Model to extend": "events - ",
            "Create this measure?": True,
            "Create this metric?": create_metric,
        }
    )
    undo: list[Any] = []
    _repl(project, "author metric", script, undo)

    metric_path = project / "metrics" / "core" / "tax_to_shipping.yml"
    assert metric_path.exists() is create_metric
    if not external_edit:
        if create_metric:
            _repl(project, "undo", None, undo)
        assert undo == [] and _authored(project) == before
        return
    # The edit between the two measures blocks restoring either one; both stay undoable.
    after = _authored(project)
    if not create_metric:
        output = capsys.readouterr().out
        assert f"conflict {project / EVENTS}" in output
        assert "kept    measure `tax`, measure `shipping`" in output
    _repl(project, "undo", None, undo)
    assert "Undo was not applied" in capsys.readouterr().out
    assert len(undo) == 1 and _authored(project) == after
    assert yaml.safe_load((project / EVENTS).read_text("utf-8"))["model"]["label"] == (
        "Externally labeled events"
    )


def test_a_ratio_refuses_the_same_input_twice(tmp_path: Path) -> None:
    project = _starter(tmp_path)
    before = _authored(project)
    script = _Script(
        {
            "Metric key": "total_per_total",
            "Metric recipe": "Ratio",
            "Numerator": "total_amount - ",
            # Creating the denominator can land on the numerator's own key.
            "Denominator": "Create a new measure first",
            "Model to extend": "events - ",
            "Measure key": "total_amount",
            "Manage and update this existing measure?": True,
            "Update this measure?": True,
        }
    )

    with pytest.raises(SemanticLayerError, match="two distinct measures or metrics"):
        _repl(project, "author metric", script, [])
    assert _authored(project) == before


@pytest.mark.parametrize("interrupt_after", ["Measure", "Metric"])
def test_an_interruption_after_any_commit_restores_every_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interrupt_after: str,
) -> None:
    project = _starter(tmp_path)
    before = _authored(project)
    interrupted = []

    def interrupt_on_success(*args: Any, **kwargs: Any) -> None:
        if not interrupted and args and str(args[0]).startswith(f"[ok] {interrupt_after}"):
            # The real project transaction has already written the files.
            interrupted.append(_authored(project) != before)
            raise KeyboardInterrupt
        print(*args, **kwargs)

    monkeypatch.setattr(authoring, "print", interrupt_on_success, raising=False)
    undo: list[Any] = []
    _repl(project, "author metric", _Script(NEW_TAX_MEASURE), undo)

    assert interrupted == [True]
    assert undo == [] and _authored(project) == before
    assert "Authoring cancelled; no files changed." in capsys.readouterr().out


def _growth(measure: str, aggregation: str, unit: str, value: int = 1) -> dict[str, Any]:
    now = {"kind": "aggregate", "measure": measure, "aggregation": aggregation}
    prior = {"kind": "prior_period", "input": now, "offset": {"unit": unit, "value": value}}
    left = {"kind": "binary", "op": "subtract", "left": now, "right": prior}
    return {
        "kind": "binary",
        "op": "divide",
        "null_behavior": "null_if_zero",
        "left": left,
        "right": prior,
    }


def _one_filter(field: str, op: str, values: list[Any]) -> dict[str, Any]:
    return {"all": [{"field": field, "op": op, "value": values}]}


# Recipe answers -> YAML that runs. Unanswered prompts take their defaults, and
# ``offered`` is what the inputs proposed: result type, currency, clock, denominator.

CREATE = [
    pytest.param(
        {}, {"Measure to publish": "revenue - "},
        {"kind": "aggregate", "measure": REVENUE, "value_type": "currency", "currency": "USD",
         "temporal_role": ORDERED, "examples": ["What is m by month?"]},
        {"Result type": "Currency", "Time axis for this metric": ABSENT},
        [10.0, 20.0, 30.0], id="aggregate-takes-the-measure-type-and-clock",
    ),
    pytest.param(
        {}, {"Measure to publish": "customer_count - "},
        {"value_type": "count", "temporal_role": ABSENT, "currency": ABSENT}, {"Result type": "Count"},
        [1], id="aggregate-without-a-clock",
    ),
    pytest.param(
        {"shipped": True}, {"Measure to publish": "tax - "},
        {"temporal_role": ORDERED}, {"Time axis for this metric": ABSENT},
        [2.0, 2.0, 2.0], id="only-the-clock-the-measure-supports",
    ),
    pytest.param(
        {"shipped": True}, {"Measure to publish": "revenue - ", "Time axis for this metric": "Shipped"},
        {"temporal_role": SHIPPED}, {"Time axis for this metric": "Ordered At (orders)"},
        [10.0, 50.0, 70.0, 50.0], id="two-clocks-ask-default-first",
    ),
    pytest.param(
        {}, {"Metric recipe": "Filtered", "Measure to publish": "revenue - ", "Values": ["completed", "placed"]},
        {"kind": "aggregate", "measure": ABSENT, "expression": {
            "kind": "aggregate", "measure": REVENUE, "aggregation": "sum",
            "filter": _one_filter("dimension.shop_order_status", "IN", ["placed", "completed"])}},
        {"Filter by": "status - [dimension] Status - orders", "Keep the rows where it": "is one of"},
        [10.0, 30.0], id="filtered",
    ),
    pytest.param(
        {}, {"Metric recipe": "Ratio", "Numerator": "revenue - "},
        {"kind": "ratio", "numerator": REVENUE, "denominator": ORDER_COUNT, "null_behavior": "null_if_zero",
         "value_type": "currency", "currency": "USD", "temporal_role": ORDERED},
        {"Denominator": ORDER_COUNT_ROW, "Result type": "Currency per unit", "Time axis for this metric": ABSENT},
        [10.0, 20.0, 30.0], id="ratio-per-count-of-the-same-model",
    ),
    pytest.param(
        {}, {"Metric recipe": "Ratio", "Numerator": "revenue - ", "Denominator": "event_count - "},
        {"temporal_role": ORDERED, "value_type": "currency"},
        {"Time axis for this metric": "Ordered At (orders)", "Result type": "Currency per unit"},
        None, id="ratio-across-models-asks-for-the-clock",
    ),
    pytest.param(
        {}, {"Metric recipe": "Ratio", "Numerator": "order_count - ", "Denominator": "event_count - ",
             "Result type": "Percent"},
        {"value_type": "percent"}, {"Result type": "Dimensionless ratio"},
        None, id="count-per-count-is-a-ratio-unless-chosen",
    ),
    pytest.param(
        {}, {"Metric recipe": "Ratio", "Numerator": "revenue - ", "Denominator": "tax - "},
        {"value_type": "ratio", "currency": ABSENT}, {"Result type": "Dimensionless ratio"},
        [10.0, 20.0, 30.0], id="same-currency-ratio-is-unitless",
    ),
    pytest.param(
        {"calendar": True}, {"Metric recipe": "Running total", "Measure": "revenue - "},
        {"kind": "cumulative", "measure": REVENUE, "value_type": "currency"}, {},
        [10.0, 30.0, 60.0], id="running-total",
    ),
    pytest.param(
        {"calendar": True}, {"Metric recipe": "Rolling", "Measure": "revenue - ", "Window length": "2"},
        {"kind": "rolling", "window": {"unit": "day", "value": 2}},
        {"Window unit": "Days", "Window length": "7"},
        [10.0, 30.0, 50.0], id="rolling-window",
    ),
    pytest.param(
        {"calendar": True}, {"Metric recipe": "Period to date", "Measure": "revenue - ", "Period": "Quarter"},
        {"kind": "period_to_date", "period": "quarter"}, {"Period": "Month to date"},
        [10.0, 30.0, 60.0], id="period-to-date",
    ),
    pytest.param(
        {"calendar": True},
        {"Metric recipe": "Prior period", "Measure": "revenue - ", "Compare with how far back": "Days"},
        {"kind": "prior_period", "offset": {"unit": "day", "value": 1}},
        {"Compare with how far back": "Months", "How many days back": "1"},
        [None, 10.0, 20.0], id="prior-period",
    ),
    pytest.param(
        {"calendar": True},
        {"Metric recipe": "Growth", "Measure": "revenue - ", "Compare with how far back": "Days"},
        {"kind": "derived", "measure": ABSENT, "expression": _growth(REVENUE, "sum", "day"),
         "value_type": "percent"},
        {"Result type": "Percent"},
        [None, 1.0, 0.5], id="growth",
    ),
]  # fmt: skip


@pytest.mark.parametrize(("shop", "answers", "fields", "offered", "values"), CREATE)
def test_recipe_answers_write_a_metric_that_runs(
    tmp_path: Path,
    shop: dict[str, bool],
    answers: dict[str, Any],
    fields: dict[str, Any],
    offered: dict[str, Any],
    values: list[Any] | None,
) -> None:
    project = _shop(tmp_path, **shop)

    script, metric = _author(project, {"Metric key": "m", **answers})

    _assert_has(metric, fields)
    _assert_has(script.offered, offered)
    if values is not None:
        assert _values(project, "m", metric.get("temporal_role")) == values


# Saved YAML -> Enter at every prompt -> the same metric, byte for byte.

ROUND_TRIP = [
    ("Aggregate", {"Measure to publish": "revenue - "}, {"Measure to publish": REVENUE_ROW}),
    ("Filtered aggregate",
     {"Measure to publish": "revenue - ", "Filter by": "channel - ", "Keep the rows where it": "is not one of",
      "Values, comma separated": "web"},
     {"Filter by": "channel - [dimension] Channel - orders", "Keep the rows where it": "is not one of",
      "Values, comma separated": "web"}),
    ("Ratio", {"Numerator": "revenue - "}, {"Numerator": REVENUE_ROW}),
    ("Running total", {"Measure": "revenue - "}, {"Measure": REVENUE_ROW}),
    ("Rolling window", {"Measure": "revenue - ", "Window unit": "Quarters", "Window length": "2"},
     {"Window unit": "Quarters", "Window length": "2"}),
    ("Period to date", {"Measure": "revenue - ", "Period": "Quarter"}, {"Period": "Quarter to date"}),
    ("Prior period", {"Measure": "revenue - ", "Compare with how far back": "Days", "How many days back": "2"},
     {"Compare with how far back": "Days", "How many days back": "2"}),
    ("Growth",
     {"Measure": "order_count - ", "Compare with how far back": "Years", "How many years back": "2",
      "Result type": "Number"},
     {"Measure": ORDER_COUNT_ROW, "Compare with how far back": "Years", "How many years back": "2",
      "Result type": "Number"}),
]  # fmt: skip


@pytest.mark.parametrize(("recipe", "answers", "offered"), ROUND_TRIP)
def test_enter_keeps_a_created_metric_byte_for_byte(
    tmp_path: Path, recipe: str, answers: dict[str, Any], offered: dict[str, Any]
) -> None:
    project = _shop(tmp_path, calendar=True)
    currency = {} if recipe == "Growth" else {"Currency code": "EUR"}
    _author(project, {"Metric key": "m", "Metric recipe": recipe, **answers, **currency})
    created, before = _path(project, "m").read_bytes(), _values(project, "m")

    script, _ = _author(project, {"Metric key": "m"})

    assert script.offered["Metric recipe"].startswith(recipe)
    _assert_has(script.offered, {**offered, **currency})
    assert _path(project, "m").read_bytes() == created
    assert _values(project, "m") == before


# An authored metric -> Enter keeps what it means, however it names its inputs;
# another input then refreshes the defaults that depend on it.


def _authored_metric(recipe: str, ref: str, *, count: str, status: str) -> dict[str, Any]:
    """Revenue by ``ref`` at AVG, in EUR, shaped like ``recipe``."""

    named = {"measure": ref, "aggregation": "avg"}
    avg = {"kind": "aggregate", **named}
    shapes: dict[str, dict[str, Any]] = {
        "aggregate": {"kind": "aggregate", **named},
        "filtered": {
            "kind": "aggregate",
            "expression": {**avg, "filter": _one_filter(status, "NOT IN", ["completed"])},
        },
        "ratio": {
            "kind": "ratio",
            "numerator": ref,
            "denominator": count,
            "null_behavior": "null_if_zero",
        },
        "cumulative": {"kind": "cumulative", **named},
        "rolling": {"kind": "rolling", **named, "window": {"unit": "quarter", "value": 2}},
        "period_to_date": {"kind": "period_to_date", **named, "period": "month"},
        "prior_period": {"kind": "prior_period", **named, "offset": {"unit": "day", "value": 1}},
        "growth": {"kind": "derived", "expression": _growth(ref, "avg", "day")},
    }
    return {**shapes[recipe], "value_type": "currency", "currency": "EUR"}


AUTHORED = [
    # recipe, how revenue is named, how the clock is written; the ratio's
    # denominator uses the order count's custom name
    ("aggregate", "revenue", "time"),
    ("filtered", "shop.revenue", "temporal_role"),
    ("ratio", "shop.revenue", "temporal_role"),
    ("cumulative", REVENUE, "temporal_role"),
    ("rolling", "revenue", "temporal_role"),
    ("period_to_date", "shop.revenue", "temporal_role"),
    ("prior_period", "revenue", "temporal_role"),
    ("growth", "shop.revenue", "temporal_role"),
]
SELECTORS = {
    "aggregate": "Measure to publish",
    "filtered": "Measure to publish",
    "ratio": "Numerator",
}


@pytest.mark.parametrize(("recipe", "ref", "clock_field"), AUTHORED)
def test_enter_keeps_an_authored_metric_and_a_new_input_refreshes_it(
    tmp_path: Path, recipe: str, ref: str, clock_field: str
) -> None:
    project = _shop(tmp_path, shipped=True, calendar=True)
    spec = _authored_metric(recipe, ref, count="shop.OrdersPlaced", status="shop.Order.status")
    path = _write_metric(project, "m", {**spec, clock_field: SHIPPED})
    original, before = path.read_bytes(), _values(project, "m", SHIPPED)
    selector = SELECTORS.get(recipe, "Measure")
    undo: list[Any] = []

    script, metric = _author(project, {"Metric key": "m"}, undo)

    canonical = _authored_metric(
        recipe, REVENUE, count=ORDER_COUNT, status="dimension.shop_order_status"
    )
    _assert_has(metric, {**canonical, "temporal_role": SHIPPED, "time": ABSENT})
    _assert_has(
        script.offered,
        {
            selector: REVENUE_ROW,
            "Result type": "Currency per unit" if recipe == "ratio" else "Currency",
            "Currency code": "EUR",
            "Time axis for this metric": "Shipped At (orders)",
        },
    )
    assert _values(project, "m", SHIPPED) == before
    _repl(project, "undo", None, undo)
    assert path.read_bytes() == original

    count = {"Numerator": "order_count - ", "Denominator": "revenue - "}
    if recipe != "ratio":
        count = {selector: "order_count - ", "Filter by": "status - ", "Values": ["completed"]}
    _, metric = _author(project, {"Metric key": "m", **count}, undo)

    value_type = {"growth": "percent", "ratio": "ratio"}.get(recipe, "count")
    _assert_has(metric, {"value_type": value_type, "currency": ABSENT, "temporal_role": ORDERED})
    if recipe in {"filtered", "growth"}:
        assert "'count_distinct'" in str(metric["expression"]) and "avg" not in str(metric)
    elif recipe != "ratio":
        _assert_has(metric, {"measure": ORDER_COUNT, "aggregation": ABSENT})
    # A window, period or offset does not depend on the measure.
    _assert_has(
        metric,
        {name: canonical[name] for name in ("window", "period", "offset") if name in canonical},
    )
    assert _values(project, "m") != before
    _repl(project, "undo", None, undo)
    assert path.read_bytes() == original and _values(project, "m", SHIPPED) == before


# A changed input or recipe -> defaults from the new input.

CHANGES = [
    pytest.param(
        {"Measure to publish": "revenue - ", "Currency code": "EUR"}, {"Measure to publish": "order_count - "},
        {"Result type": "Count"}, {"measure": ORDER_COUNT, "value_type": "count", "currency": ABSENT},
        [1, 1, 1], id="measure-to-a-count",
    ),
    pytest.param(
        {"Measure to publish": "order_count - "}, {"Measure to publish": "revenue - "},
        {"Result type": "Currency", "Currency code": "USD"}, {"value_type": "currency", "currency": "USD"},
        [10.0, 20.0, 30.0], id="measure-to-currency",
    ),
    pytest.param(
        {"Measure to publish": "revenue - ", "Currency code": "EUR"},
        {"Measure to publish": "order_count - ", "Result type": "Number"},
        {"Result type": "Count"}, {"value_type": "number", "currency": ABSENT},
        [1, 1, 1], id="refreshed-type-can-be-overridden",
    ),
    pytest.param(
        {"Metric recipe": "Ratio", "Numerator": "order_count - ", "Denominator": "revenue - "},
        {"Numerator": "revenue - ", "Denominator": "order_count - "},
        {"Result type": "Currency per unit"}, {"numerator": REVENUE, "value_type": "currency", "currency": "USD"},
        [10.0, 20.0, 30.0], id="ratio-operands-swapped",
    ),
    pytest.param(
        {"Metric recipe": "Ratio", "Numerator": "revenue - ", "Denominator": "tax - "},
        {"Denominator": "order_count - "},
        {"Numerator": REVENUE_ROW, "Result type": "Currency per unit"},
        {"denominator": ORDER_COUNT, "value_type": "currency", "currency": "USD"},
        [10.0, 20.0, 30.0], id="ratio-denominator-only",
    ),
    pytest.param(
        {"Metric recipe": "Rolling window", "Measure": "revenue - "},
        {"Metric recipe": "Aggregate", "Measure to publish": "revenue - "},
        {}, {"kind": "aggregate", "window": ABSENT},
        [10.0, 20.0, 30.0], id="recipe-switch-drops-old-fields",
    ),
    pytest.param(
        {"Measure to publish": "revenue - ", "Currency code": "EUR"},
        {"Metric recipe": "Growth", "Measure": "revenue - ", "Compare with how far back": "Days"},
        {"Result type": "Percent"}, {"kind": "derived", "value_type": "percent", "currency": ABSENT},
        [None, 1.0, 0.5], id="aggregate-to-growth",
    ),
    pytest.param(
        {"Metric recipe": "Growth", "Measure": "revenue - ", "Compare with how far back": "Days",
         "Result type": "Currency", "Currency code": "EUR"},
        {"Metric recipe": "Prior period"},
        {"Result type": "Currency", "Currency code": "USD", "Compare with how far back": "Days"},
        {"kind": "prior_period", "value_type": "currency", "currency": "USD"},
        [None, 10.0, 20.0], id="growth-to-prior-period",
    ),
]  # fmt: skip


@pytest.mark.parametrize(("create", "change", "offered", "fields", "values"), CHANGES)
def test_a_changed_input_refreshes_its_defaults_and_undoes(
    tmp_path: Path,
    create: dict[str, Any],
    change: dict[str, Any],
    offered: dict[str, Any],
    fields: dict[str, Any],
    values: list[Any],
) -> None:
    project = _shop(tmp_path, calendar=True)
    _author(project, {"Metric key": "m", **create})
    created, before = _path(project, "m").read_bytes(), _values(project, "m")
    undo: list[Any] = []

    script, metric = _author(project, {"Metric key": "m", **change}, undo)

    _assert_has(script.offered, offered)
    _assert_has(metric, fields)
    assert _values(project, "m") == pytest.approx(values)
    _repl(project, "undo", None, undo)
    assert _path(project, "m").read_bytes() == created and _values(project, "m") == before


def test_an_explicit_clock_replaces_a_saved_time_alias(tmp_path: Path) -> None:
    project = _shop(tmp_path, shipped=True)
    spec = _authored_metric("aggregate", "revenue", count="", status="")
    _write_metric(project, "m", {**spec, "time": SHIPPED})

    script, metric = _author(project, {"Metric key": "m", "Time axis for this metric": "Ordered"})

    assert script.offered["Time axis for this metric"] == "Shipped At (orders)"
    _assert_has(metric, {"temporal_role": ORDERED, "time": ABSENT, "aggregation": "avg"})
    assert _values(project, "m") == [20.0, 30.0, 40.0]


# Filters: values keep their declared type through create and Enter.

FILTERS = [
    # dimension, values picked or typed, operator, stored values, total, what Enter offers again
    ("status", ["completed", "placed"], "IN", ["placed", "completed"], 40.0, ["0", "1"]),
    ("stage", ["Completed orders (completed)"], "IN", ["completed"], 10.0, ["1"]),
    ("channel", "phone, store", "NOT IN", ["phone", "store"], 10.0, "phone, store"),
    ("code", "001, true", "IN", ["001", "true"], 30.0, "001, true"),
    ("is_priority", "true", "IN", [True], 40.0, "true"),
    ("tier", "001", "IN", [1], 30.0, "1"),
]
OPERATORS = {"IN": "is one of", "NOT IN": "is not one of"}


@pytest.mark.parametrize(("dimension", "given", "op", "stored", "total", "offered"), FILTERS)
def test_filter_values_keep_their_type_through_create_and_enter(
    tmp_path: Path,
    dimension: str,
    given: Any,
    op: str,
    stored: list[Any],
    total: float,
    offered: Any,
) -> None:
    project = _shop(tmp_path)
    values = "Values" if isinstance(given, list) else "Values, comma separated"
    filtered = {"Metric recipe": "Filtered", "Measure to publish": "revenue - "}
    answers = {
        "Filter by": f"{dimension} - ",
        "Keep the rows where it": OPERATORS[op],
        values: given,
    }

    script, metric = _author(project, {"Metric key": "m", **filtered, **answers})

    clause = _one_filter(f"dimension.shop_order_{dimension}", op, stored)
    assert metric["expression"]["filter"] == clause
    assert list(map(type, clause["all"][0]["value"])) == list(map(type, stored))
    assert _values(project, "m", None) == [total]
    created = _path(project, "m").read_bytes()

    script, _ = _author(project, {"Metric key": "m"})

    _assert_has(script.offered, {"Keep the rows where it": OPERATORS[op], values: offered})
    assert _path(project, "m").read_bytes() == created


@pytest.mark.parametrize(("dimension", "typed"), [("is_priority", "maybe"), ("tier", "1.5")])
def test_typed_filter_values_refuse_other_types(tmp_path: Path, dimension: str, typed: str) -> None:
    project = _shop(tmp_path)
    filtered = {"Metric recipe": "Filtered", "Measure to publish": "revenue - "}
    answers = {"Filter by": f"{dimension} - ", "Values, comma separated": typed}

    with pytest.raises(SemanticLayerError, match="filter value"):
        _author(project, {"Metric key": "m", **filtered, **answers})
    assert not _path(project, "m").exists()


def test_another_filter_dimension_or_measure_asks_for_new_values(tmp_path: Path) -> None:
    project = _shop(tmp_path)
    filtered = {"Metric key": "m", "Metric recipe": "Filtered", "Measure to publish": "revenue - "}
    no_web = {
        "Filter by": "channel - ",
        "Keep the rows where it": "is not",
        "Values, comma separated": "web",
    }
    _author(project, {**filtered, **no_web})
    created = _path(project, "m").read_bytes()
    assert _values(project, "m") == [20.0, 30.0]

    undo: list[Any] = []
    completed = {"Filter by": "status - ", "Values": ["completed"]}
    script, metric = _author(project, {"Metric key": "m", **completed}, undo)

    assert script.offered["Metric recipe"].startswith("Filtered aggregate")
    _assert_has(
        script.offered,
        {
            "Measure to publish": REVENUE_ROW,
            "Filter by": "channel - [dimension] Channel - orders",
            "Keep the rows where it": "is one of",
            "Values": [],
        },
    )
    assert metric["expression"]["filter"] == _one_filter(
        "dimension.shop_order_status", "IN", ["completed"]
    )
    assert _values(project, "m") == [10.0]
    _repl(project, "undo", None, undo)
    assert _path(project, "m").read_bytes() == created

    # Another measure offers no filter: the old one may not apply to it.
    with pytest.raises(SemanticLayerError, match="Choose filter by"):
        _author(project, {"Metric key": "m", "Measure to publish": "order_count - "})
    assert _path(project, "m").read_bytes() == created
    counted = {"Metric key": "m", "Measure to publish": "order_count - ", **completed}
    script, metric = _author(project, counted)
    _assert_has(script.offered, {"Filter by": "", "Values": [], "Result type": "Count"})
    assert metric["expression"]["aggregation"] == "count_distinct"
    _assert_has(metric, {"value_type": "count", "currency": ABSENT})
    assert _values(project, "m") == [1]


# Expressions stay as authored, even where a recipe could write them, until
# another recipe is chosen explicitly.

AVG = {"kind": "aggregate", "measure": "revenue", "aggregation": "avg"}
FILTERED_AVG = {**AVG, "filter": _one_filter("dimension.shop_order_status", "NOT IN", ["returned"])}
TWO_FILTERS = {
    "all": [
        *FILTERED_AVG["filter"]["all"],
        {"field": "dimension.shop_order_channel", "op": "IN", "value": ["web"]},
    ]
}
PRIOR_DAY = {"kind": "prior_period", "input": FILTERED_AVG, "offset": {"unit": "day", "value": 1}}
GROWTH_OVER_A_FILTER = {
    **_growth("revenue", "avg", "day"),
    "left": {"kind": "binary", "op": "subtract", "left": FILTERED_AVG, "right": PRIOR_DAY},
    "right": PRIOR_DAY,
}
KEPT = [
    pytest.param("derived", {"kind": "binary", "op": "multiply", "left": AVG, "right": {"kind": "literal", "value": 2}}, id="derived"),
    pytest.param("aggregate", {**FILTERED_AVG, "filter": TWO_FILTERS}, id="two-filter-clauses"),
    pytest.param("rolling", {"kind": "rolling", "input": AVG, "window": {"unit": "day", "value": 2}}, id="rolling"),
    pytest.param("prior_period", PRIOR_DAY, id="prior-period-over-a-filter"),
    pytest.param("period_to_date", {"kind": "period_to_date", "input": FILTERED_AVG, "period": "month"}, id="to-date"),
    pytest.param("cumulative", {"kind": "cumulative", "input": AVG}, id="cumulative"),
    pytest.param("derived", GROWTH_OVER_A_FILTER, id="growth-over-a-filter"),
]  # fmt: skip


@pytest.mark.parametrize(("kind", "expression"), KEPT)
def test_enter_keeps_an_authored_expression(
    tmp_path: Path, kind: str, expression: dict[str, Any]
) -> None:
    project = _shop(tmp_path, calendar=True)
    spec = {
        "kind": kind,
        "expression": expression,
        "temporal_role": ORDERED,
        "value_type": "number",
    }
    path = _write_metric(project, "m", spec)
    before = _values(project, "m")

    script, metric = _author(project, {"Metric key": "m"})

    what = "derived expression" if kind == "derived" else "expression"
    assert script.offered["Metric recipe"] == f"Keep this {what} unchanged"
    assert "Result type" not in script.offered
    assert metric["expression"] == expression and _values(project, "m") == before

    kept, undo = path.read_bytes(), []
    rolling = {"Metric recipe": "Rolling window", "Measure": "revenue - "}
    _, metric = _author(project, {"Metric key": "m", **rolling}, undo)
    _assert_has(
        metric, {"kind": "rolling", "expression": ABSENT, "window": {"unit": "day", "value": 7}}
    )
    _repl(project, "undo", None, undo)
    assert path.read_bytes() == kept


def test_growth_is_kept_while_the_package_has_no_calendar(tmp_path: Path) -> None:
    project = _shop(tmp_path)
    growth = {
        "kind": "derived",
        "expression": _growth(REVENUE, "sum", "day"),
        "value_type": "percent",
    }
    _write_metric(project, "m", {**growth, "temporal_role": ORDERED})

    script, metric = _author(project, {"Metric key": "m"})

    assert script.offered["Metric recipe"] == "Keep this derived expression unchanged"
    assert metric["expression"] == growth["expression"]


def test_a_saved_unit_the_wizard_cannot_offer_is_refused_not_replaced(tmp_path: Path) -> None:
    project = _shop(tmp_path, calendar=True)
    spec = {"kind": "rolling", "measure": "revenue", "window": {"unit": "hour", "value": 2}}
    path = _write_metric(project, "m", {**spec, "temporal_role": ORDERED, "value_type": "number"})
    original = path.read_bytes()

    with pytest.raises(SemanticLayerError, match="not supported by this wizard"):
        _author(project, {"Metric key": "m"})
    assert path.read_bytes() == original


# What each recipe needs from the package.


def test_calendar_recipes_appear_only_with_a_calendar(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script, _ = _author(_shop(tmp_path), {"Metric key": "m", "Measure to publish": "revenue - "})

    recipes = [option.split(" - ")[0] for option in script.options["Metric recipe"]]
    assert recipes == [
        "Aggregate",
        "Filtered aggregate",
        "Ratio",
        "Running total",
        "Period to date",
    ]
    assert "need a calendar table in the package" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("recipe", "message"),
    [("Running total", "Running total needs a time axis"), ("Filtered", "no dimension to filter")],
)
def test_a_recipe_refuses_a_measure_it_cannot_read(
    tmp_path: Path, recipe: str, message: str
) -> None:
    project = _shop(tmp_path)
    answers = {"Measure": "customer_count - ", "Measure to publish": "customer_count - "}

    with pytest.raises(SemanticLayerError, match=message):
        _author(project, {"Metric key": "m", "Metric recipe": recipe, **answers})
    assert not _path(project, "m").exists()


# The real Plain and Picker backends: Enter accepts each saved choice.


@contextmanager
def _real_backend(
    ui: str, monkeypatch: pytest.MonkeyPatch, key: str, search: str = "", kind: str = "metric"
) -> Iterator[dict[str, tuple[list[tuple[str, str]], str]]]:
    """Type the ``kind``'s key, confirm the write, and press Enter everywhere else.

    Where nothing is offered, take the first option; Plain prompts type
    ``search`` at list filters. Yields each choice's options and default by label.
    """

    seen: dict[str, tuple[list[tuple[str, str]], str]] = {}
    asked = itertools.count()

    def record(label: str, options: Sequence[tuple[str, str]] = (), default: str = "") -> None:
        # A wizard that keeps asking fails the test instead of hanging it.
        assert next(asked) < 60, f"still asking {label!r}"
        if options:
            seen[label] = (list(options), default)

    if ui == "plain":

        def reply(prompt: str = "") -> str:
            record(prompt)
            if prompt.startswith(f"{kind.title()} key"):
                return key
            if prompt.startswith(("Manage and update", f"Update this {kind}?")):
                return "y"
            if prompt.startswith("Filter "):
                return search
            return "1" if prompt.startswith("Choose") and "[" not in prompt else ""

        class Plain(backend.PlainBackend):
            def choose(
                self, label: str, options: Sequence[tuple[str, str]], *, default: str = ""
            ) -> str:
                record(label, options, default)
                return super().choose(label, options, default=default)

        monkeypatch.setattr("builtins.input", reply)
        backend.set_backend(Plain())
        yield seen
        return

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:

        class Picker(backend.PickerBackend):
            def text(self, label: str, *, default: str = "") -> str:
                record(label)
                pipe.send_text(("\x15" + key if label == f"{kind.title()} key" else "") + "\r")
                return super().text(label, default=default)

            def confirm(self, label: str, *, default: bool) -> bool:
                record(label)
                pipe.send_text("y\r")
                return super().confirm(label, default=default)

            def choose(
                self, label: str, options: Sequence[tuple[str, str]], *, default: str = ""
            ) -> str:
                record(label, options, default)
                pipe.send_text("\r")
                return super().choose(label, options, default=default)

            def multi_choose(
                self,
                label: str,
                options: Sequence[tuple[str, str]],
                *,
                defaults: Collection[str] = (),
            ) -> list[str]:
                record(label)
                pipe.send_text("\r" if defaults else " \r")
                return super().multi_choose(label, options, defaults=defaults)

        backend.set_backend(Picker(input=pipe, output=DummyOutput()))
        yield seen


REAL = [
    # ui, recipe, 13 more measures before revenue, what Plain types at the list filter
    *((ui, recipe, recipe in {"aggregate", "rolling"}, "") for ui in ("plain", "pickers")
      for recipe in ("aggregate", "ratio", "rolling", "filtered", "kept")),
    ("plain", "aggregate", True, "revenue"),
    ("plain", "aggregate", True, "other"),
]  # fmt: skip


@pytest.mark.parametrize(("ui", "recipe", "long_list", "search"), REAL)
def test_real_backends_keep_the_saved_metric_on_enter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ui: str,
    recipe: str,
    long_list: bool,
    search: str,
) -> None:
    """The saved canonical ID wins over an earlier measure whose key spells it."""

    project = _shop(tmp_path, shipped=True, calendar=True)
    orders = yaml.safe_load((project / "models" / "core" / "orders.yml").read_text("utf-8"))
    revenue = orders["model"]["measures"]["revenue"]
    decoy = {**revenue, "as": "measure.shop.other", "label": "Other", "expr": "subtotal * 10"}
    fillers = {
        f"filler_{index}": {**revenue, "label": f"Revenue filler {index}"} for index in range(13)
    }
    measures = {REVENUE: decoy, **(fillers if long_list else {}), **orders["model"]["measures"]}
    _write_yaml(
        project / "models" / "core" / "orders.yml",
        {"model": {**orders["model"], "measures": measures}},
    )
    if recipe == "kept":
        spec = canonical = {
            "kind": "derived",
            "expression": GROWTH_OVER_A_FILTER,
            "value_type": "percent",
        }
    else:
        spec = _authored_metric(recipe, "revenue", count="order_count", status="shop.Order.status")
        canonical = _authored_metric(
            recipe, REVENUE, count=ORDER_COUNT, status="dimension.shop_order_status"
        )
    path = _write_metric(project, "m", {**spec, "temporal_role": SHIPPED})
    original, before = path.read_bytes(), _values(project, "m", SHIPPED)
    undo: list[Any] = []

    with _real_backend(ui, monkeypatch, "m", search) as seen:
        _repl(project, "author metric", None, undo)

    assert len(undo) == 1 and undo[0].report["parse"]["ok"] is True
    metric = _saved(project, "m")
    offered = {label: dict(options).get(default, "") for label, (options, default) in seen.items()}
    selector = SELECTORS.get(recipe, "Measure")
    if recipe != "kept":
        texts = [text for _, text in seen[selector][0]]
        assert "measure.shop.revenue - [measure] Other - orders" in texts
        assert len(texts) == 13 or search != "revenue"  # the first 12 matches, then "create"
    if search == "other":
        # The search left out the saved measure: nothing is offered, and the
        # chosen measure brings its own defaults.
        assert offered[selector] == "" and REVENUE_ROW not in texts
        _assert_has(
            metric, {"measure": "measure.shop.other", "aggregation": ABSENT, "currency": "USD"}
        )
        assert _values(project, "m") == [400.0, 600.0, 800.0]
    else:
        expected = {
            "aggregate": {selector: REVENUE_ROW},
            "ratio": {selector: REVENUE_ROW, "Denominator": ORDER_COUNT_ROW},
            "rolling": {selector: REVENUE_ROW, "Window unit": "Quarters"},
            "filtered": {selector: REVENUE_ROW, "Keep the rows where it": "is not one of"},
            "kept": {"Metric recipe": "Keep this derived expression unchanged"},
        }
        _assert_has(offered, expected[recipe])
        _assert_has(metric, {**canonical, "temporal_role": SHIPPED})
        assert _values(project, "m", SHIPPED) == before
    _repl(project, "undo", None, undo)
    assert undo == [] and path.read_bytes() == original


# Other authoring defaults.


def test_editing_a_metric_keeps_its_authored_examples(tmp_path: Path) -> None:
    project = _shop(tmp_path)
    _author(project, {"Metric key": "m", "Measure to publish": "revenue - "})
    examples = ["Revenue by channel last week?", "Which day had the most revenue?"]
    doc = yaml.safe_load(_path(project, "m").read_text("utf-8"))
    doc["metrics"]["m"]["examples"] = examples
    _write_yaml(_path(project, "m"), doc)

    _, metric = _author(project, {"Metric key": "m", "Measure to publish": "order_count - "})

    assert (metric["measure"], metric["examples"]) == (ORDER_COUNT, examples)


def test_growth_offers_the_units_its_calendar_can_fill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from semantic_rails.cli.reports import ask_report

    project = _shop(tmp_path, calendar=True)
    calendar = project / "models" / "core" / "calendar.yml"
    doc = yaml.safe_load(calendar.read_text("utf-8"))
    del doc["model"]["dimensions"]["month_start"]
    _write_yaml(calendar, doc)

    script, _ = _author(
        project, {"Metric key": "g", "Metric recipe": "Growth", "Measure": "revenue - "}
    )

    assert script.options["Compare with how far back"] == ["Days", "Weeks", "Quarters", "Years"]
    assert script.offered["Compare with how far back"] == "Days"
    assert "columns on the calendar model: `month_start`." in capsys.readouterr().out
    # One written by hand says why it cannot run, instead of returning nothing.
    growth = {"kind": "derived", "expression": _growth(REVENUE, "sum", "month")}
    _write_metric(project, "g", {**growth, "value_type": "percent", "temporal_role": ORDERED})
    report = ask_report(
        PackageReference(source_path=str(project)), question="g by month", execute=True
    )
    assert [error["message"] for error in report["errors"]] == [
        "time.fill requires calendar dimension 'month_start' on 'entity.shop_time'"
    ]


def test_a_taken_or_similar_key_asks_again_instead_of_ending_the_wizard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _starter(tmp_path)
    architect = authoring.ArchitectProject(project, workspace_root=tmp_path)
    replies = iter(
        [
            *("total_amount", "n"),  # the measure exists; No: don't update it
            *("total_amount_2", ""),  # "Total Amount 2" sounds like "Total amount"...
            "",  # ...so Enter: choose a different key and label
            *("gross_sales", ""),
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    backend.set_backend(backend.PlainBackend())

    identity = authoring._author_identity(
        architect, architect.inventory(), "measure", "revenue", parent="events"
    )

    assert identity == ("gross_sales", "Gross Sales", None)


@pytest.mark.parametrize(
    ("model", "entity"),
    [
        ("orders", "order"),
        ("categories", "category"),
        ("addresses", "address"),
        ("status", "status"),
    ],
)
def test_a_new_model_proposes_its_singular_as_the_entity(
    tmp_path: Path, model: str, entity: str
) -> None:
    script = _Script({"Model key": model, "Create this model?": False})

    _repl(_starter(tmp_path), "author model", script, [])

    assert script.offered["Business entity at one row of this model"] == entity
    assert script.offered["Primary key column(s), comma separated"] == f"{entity}_id"


KEPT_CHOICES = [
    # command, key, fields saved over the starter's, fields Enter writes back (None: same bytes)
    ("measure", "total_amount", {"default_agg": "median"}, None),
    ("measure", "total_amount", {"default_agg": "percentile"}, None),
    ("measure", "total_amount", {"accumulation": {"kind": "stock"}, "default_agg": "last_value"}, None),
    ("measure", "total_amount", {"accumulation": {"kind": "stock", "snapshot": "start_of_period"},
                                 "default_agg": ABSENT}, {"default_agg": "first_value"}),
    ("measure", "event_count", {"accumulation": {"kind": "population"}}, None),
    ("measure", "event_count", {"kind": "Entity_Count", "accumulation": {"kind": "population"}},
     {"kind": "entity_count"}),
    ("measure", "total_amount", {"default_agg": "MEDIAN", "value_type": "ratio"}, {"default_agg": "median"}),
    ("dimension", "event_type", {"kind": "number", "description": "Event type."}, None),
    ("time", "occurred_at", {"kind": "Date", "class": "State_Time", "default_query_axis": True},
     {"kind": "date", "class": "state_time"}),
    ("metric", "m", {"value_type": "ratio"}, None),
]  # fmt: skip


@pytest.mark.parametrize("ui", ["plain", "pickers"])
@pytest.mark.parametrize(("command", "key", "saved", "written"), KEPT_CHOICES)
def test_enter_keeps_a_saved_choice_the_menu_does_not_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ui: str,
    command: str,
    key: str,
    saved: dict[str, Any],
    written: dict[str, Any] | None,
) -> None:
    project = _starter(tmp_path)
    if command == "metric":
        _author(project, {"Metric key": key, "Measure to publish": "total_amount - "})
    path = _path(project, key) if command == "metric" else project / EVENTS

    def spec() -> tuple[dict[str, Any], dict[str, Any]]:
        doc = yaml.safe_load(path.read_text("utf-8"))
        return doc, (doc["metrics"] if command == "metric" else doc["model"][f"{command}s"])[key]

    doc, before = spec()
    before.update(saved)
    for name in [name for name, value in saved.items() if value is ABSENT]:
        del before[name]
    _write_yaml(path, doc)
    original, undo = path.read_bytes(), list[Any]()

    with _real_backend(ui, monkeypatch, key, kind=command):
        _repl(project, f"author {command}", None, undo)

    assert len(undo) == 1 and undo[0].report["parse"]["ok"] is True
    if written is None:
        assert path.read_bytes() == original
    else:
        assert spec()[1] == {**before, **written}


@pytest.mark.parametrize(("strict", "aggregation"), [(True, None), (True, "Sum"), (False, "Sum")])
def test_a_new_default_aggregation_names_the_metrics_it_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], strict: bool, aggregation: str | None
) -> None:
    project = _starter(tmp_path)
    events = yaml.safe_load((project / EVENTS).read_text("utf-8"))
    events["model"]["measures"]["total_amount"]["default_agg"] = "median"
    _write_yaml(project / EVENTS, events)
    if not strict:
        # The loader publishes metric.shop.total_amount itself, spelling out the median.
        package = yaml.safe_load((project / "package.yml").read_text("utf-8"))
        package["package"]["schema_strict"] = False
        _write_yaml(project / "package.yml", package)
        (project / "metrics" / "core.yml").unlink()
    ratio = {"numerator": "metric.shop.total_amount", "denominator": "measure.shop.event_count"}
    _write_metric(project, "per_event", {"kind": "ratio", **ratio, "value_type": "ratio"})
    ratio = {"numerator": "metric.shop.per_event", "denominator": "measure.shop.event_count"}
    _write_metric(project, "per_event_2", {"kind": "ratio", **ratio, "value_type": "ratio"})
    scoped = {"kind": "scoped_aggregate", "measure": "measure.shop.total_amount"}
    _write_metric(
        project, "scoped", {"kind": "derived", "expression": scoped, "value_type": "number"}
    )
    top = {"kind": "aggregate", "measure": "total_amount", "aggregation": "max"}
    _write_metric(project, "top", {**top, "value_type": "number"})
    confirm = ("Manage and update this existing measure?", "Update this measure?")
    answers = {"Measure key": "total_amount", **dict.fromkeys(confirm, True)}
    script = _Script({**answers, "Default aggregation": aggregation})

    _repl(project, "author measure", script, [])

    out = capsys.readouterr().out
    assert script.offered["Default aggregation"] == "Median"
    if aggregation is None:
        assert "[warning] This changes the default aggregation" not in out
    else:
        assert "[warning] This changes the default aggregation from median to sum." in out
        # The metrics on the measure's default and those built on them; not `top`.
        changed = "per_event, per_event_2, scoped, total_amount"
        assert f"different numbers: metric.shop.{changed.replace(', ', ', metric.shop.')}\n" in out
