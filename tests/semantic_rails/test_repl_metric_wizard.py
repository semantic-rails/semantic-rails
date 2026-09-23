"""The REPL metric wizard picks defaults from the metric's own inputs.

A metric used to take the package's default clock, so a metric on orders
was reported on the starter's event time, and a ratio's denominator and
result type defaulted to whatever came first. Now the inputs decide: the
clock is the input model's, the denominator defaults to a count on the
numerator's model, and revenue per order defaults to currency.
"""

from __future__ import annotations

import sys
from collections.abc import Collection, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from semantic_rails.cli import scaffold
from semantic_rails.config_validation import PackageReference
from semantic_rails.repl import backend, shell

META = {"owner_team": "analytics", "review_priority": "medium", "change_risk": "low"}


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

    def text(self, label: str, *, default: str = "") -> str:
        self.offered[label] = default
        return str(self.answers.get(label, default))

    def confirm(self, label: str, *, default: bool) -> bool:
        self.offered[label] = default
        return bool(self.answers.get(label, default))

    def choose(self, label: str, options: Sequence[tuple[str, str]], *, default: str = "") -> str:
        described = dict(options)
        self.offered[label] = described.get(default, "")
        self.options[label] = list(described.values())
        wanted = self.answers.get(label)
        if wanted is None:
            return default
        return next(value for value, text in options if str(wanted) in text)

    def multi_choose(
        self, label: str, options: Sequence[tuple[str, str]], *, defaults: Collection[str] = ()
    ) -> list[str]:
        return list(defaults)

    def show_yaml(self, payload: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    yield
    backend.set_backend(None)


def _shop(
    tmp_path: Path, *, order_times: tuple[str, ...] = ("ordered_at",), calendar: bool = False
) -> Path:
    """The starter package (events, on occurred_at) plus an orders model and a customers model.

    ``calendar`` adds a date spine, which rolling windows and prior periods need.
    """

    project = Path(
        scaffold.create_project_report(
            package_id="shop", workspace_root=str(tmp_path), run_checks=False
        )["project_path"]
    )
    times = {
        column: {
            "label": column.replace("_", " ").title(),
            "column": column,
            "kind": "timestamp",
            "class": "event_time",
            **({"default": True} if index == 0 else {}),
        }
        for index, column in enumerate(order_times)
    }
    models = {
        "orders": {
            "id": "orders",
            "label": "Orders",
            "description": "One row per order.",
            "relation": "raw_orders",
            "entities": {"order": {}},
            "times": times,
            "measures": {
                "revenue": {
                    "label": "Revenue",
                    "kind": "aggregate",
                    "expr": "subtotal",
                    "default_agg": "sum",
                    "accumulation": {"kind": "flow"},
                    "value_type": "currency",
                    "currency": "USD",
                    "meta": META,
                },
                "order_count": {
                    "label": "Order count",
                    "kind": "entity_count",
                    "entity_key": "order_id",
                    "accumulation": {"kind": "event"},
                    "value_type": "count",
                    "meta": META,
                },
            },
        },
        "customers": {
            "id": "customers",
            "label": "Customers",
            "description": "One row per customer.",
            "relation": "raw_customers",
            "entities": {"customer": {}},
            "measures": {
                "customer_count": {
                    "label": "Customer count",
                    "kind": "entity_count",
                    "entity_key": "customer_id",
                    "accumulation": {"kind": "event"},
                    "value_type": "count",
                    "meta": META,
                },
            },
        },
    }
    graph = yaml.safe_load((project / "graph.yml").read_text(encoding="utf-8"))
    for model_id, model in models.items():
        (project / "models" / "core" / f"{model_id}.yml").write_text(
            yaml.safe_dump({"model": model}, sort_keys=False), encoding="utf-8"
        )
        entity = next(iter(model["entities"]))
        graph["graph"]["entities"][entity] = {
            "label": entity.title(),
            "key": [f"{entity}_id"],
            "model": model_id,
            "allowed_as_root": True,
        }
    if calendar:
        _add_calendar(project, graph)
    (project / "graph.yml").write_text(yaml.safe_dump(graph, sort_keys=False), encoding="utf-8")
    return project


def _add_calendar(project: Path, graph: dict[str, Any]) -> None:
    model = {
        "id": "calendar",
        "relation": "calendar",
        "calendar_id": "default",
        "entities": {"time": {}},
        "times": {
            "date_day": {
                "label": "Calendar day",
                "column": "date_day",
                "kind": "date",
                "class": "calendar_time",
            }
        },
        "dimensions": {
            f"{grain}_start": {"label": f"Calendar {grain} start", "kind": "date"}
            for grain in ("week", "month", "quarter", "year")
        },
    }
    (project / "models" / "core" / "calendar.yml").write_text(
        yaml.safe_dump({"model": model}, sort_keys=False), encoding="utf-8"
    )
    graph["graph"]["entities"]["time"] = {
        "label": "Calendar",
        "kind": "time",
        "key": ["date_day"],
        "model": "calendar",
        "allowed_as_root": False,
    }


def _author_metric(project: Path, answers: dict[str, Any]) -> tuple[_Script, dict[str, Any]]:
    script = _Script({"Create this metric?": True, **answers})
    backend.set_backend(script)
    undo: list[Any] = []
    shell._handle_repl_line(
        "author metric", PackageReference(source_path=str(project)), undo_stack=undo
    )
    assert undo and undo[0].report["parse"]["ok"] is True, "the metric was written and parses"
    key = answers["Metric key"]
    path = project / "metrics" / "core" / f"{key}.yml"
    return script, yaml.safe_load(path.read_text(encoding="utf-8"))["metrics"][key]


def test_a_ratio_takes_its_denominator_type_and_clock_from_the_numerator(tmp_path: Path) -> None:
    project = _shop(tmp_path)

    script, metric = _author_metric(
        project, {"Metric key": "aov", "Metric recipe": "Ratio", "Numerator": "revenue - "}
    )

    assert script.offered["Denominator"].startswith("order_count - ")
    assert script.offered["Result type"] == "Currency per unit"
    assert metric["numerator"] == "measure.shop.revenue"
    assert metric["denominator"] == "measure.shop.order_count"
    assert (metric["value_type"], metric["currency"]) == ("currency", "USD")
    assert metric["temporal_role"] == "temporal_role.shop_order_ordered_at"
    assert "Time axis for this metric" not in script.offered  # one clock, nothing to ask


def test_an_aggregate_takes_its_measures_clock_and_type_not_the_starters(tmp_path: Path) -> None:
    project = _shop(tmp_path)

    script, metric = _author_metric(
        project, {"Metric key": "gross_revenue", "Measure to publish": "revenue - "}
    )

    assert metric["temporal_role"] == "temporal_role.shop_order_ordered_at"
    assert script.offered["Result type"] == "Currency"
    assert metric["value_type"] == "currency"


def test_a_measure_on_a_model_without_a_clock_gets_none(tmp_path: Path) -> None:
    project = _shop(tmp_path)

    _, metric = _author_metric(
        project, {"Metric key": "customers_total", "Measure to publish": "customer_count - "}
    )

    assert "temporal_role" not in metric  # not the starter's event clock
    assert metric["value_type"] == "count"


def test_a_model_with_several_clocks_asks_which_one(tmp_path: Path) -> None:
    project = _shop(tmp_path, order_times=("ordered_at", "shipped_at"))

    script, metric = _author_metric(
        project,
        {
            "Metric key": "shipped_revenue",
            "Measure to publish": "revenue - ",
            "Time axis for this metric": "Shipped At",
        },
    )

    assert script.offered["Time axis for this metric"] == "Ordered At (orders)"
    assert script.options["Time axis for this metric"] == [
        "Ordered At (orders)",
        "Shipped At (orders)",
    ]
    assert metric["temporal_role"] == "temporal_role.shop_order_shipped_at"


def test_a_ratio_across_models_asks_for_the_clock_and_defaults_to_the_numerators(
    tmp_path: Path,
) -> None:
    project = _shop(tmp_path)

    script, metric = _author_metric(
        project,
        {
            "Metric key": "revenue_per_event",
            "Metric recipe": "Ratio",
            "Numerator": "revenue - ",
            "Denominator": "event_count - ",
        },
    )

    assert script.offered["Time axis for this metric"] == "Ordered At (orders)"
    assert metric["temporal_role"] == "temporal_role.shop_order_ordered_at"
    assert script.offered["Result type"] == "Currency per unit"


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [
        ("currency", "count", "currency"),
        ("currency", "number", "currency"),
        ("count", "count", "percent"),
        ("currency", "currency", "percent"),
        ("number", "count", "ratio"),
        ("", "", "ratio"),
    ],
)
def test_ratio_result_type_defaults(numerator: str, denominator: str, expected: str) -> None:
    from semantic_rails.repl.authoring import _ratio_value_type

    def row(value_type: str) -> dict[str, Any]:
        return {"kind": "measure", "spec": {"value_type": value_type}}

    assert _ratio_value_type(row(numerator), row(denominator)) == expected


def _tree_bytes(folder: Path) -> dict[str, bytes]:
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file() and ".architect" not in path.parts
    }


NEW_TAX_MEASURE = {
    "Measure to publish": "Create a new measure first",
    "Model to extend": "orders - ",
    "Measure key": "tax",
    "Column or scalar expression (for example amount_cents / 100.0)": "tax_paid",
    "Result type": "Currency",
    "Create this measure?": True,
}


def test_a_metric_can_create_the_measure_it_needs_and_one_undo_takes_both_back(
    tmp_path: Path,
) -> None:
    project = _shop(tmp_path)
    before = _tree_bytes(project)
    script = _Script(
        {"Metric key": "tax_collected", "Create this metric?": True, **NEW_TAX_MEASURE}
    )
    backend.set_backend(script)
    ref = PackageReference(source_path=str(project))
    undo: list[Any] = []

    shell._handle_repl_line("author metric", ref, undo_stack=undo)

    orders = yaml.safe_load((project / "models" / "core" / "orders.yml").read_text("utf-8"))
    metric = yaml.safe_load((project / "metrics" / "core" / "tax_collected.yml").read_text("utf-8"))
    assert orders["model"]["measures"]["tax"]["expr"] == "tax_paid"
    assert metric["metrics"]["tax_collected"]["measure"] == "measure.shop.tax"
    assert (
        metric["metrics"]["tax_collected"]["temporal_role"] == "temporal_role.shop_order_ordered_at"
    )
    assert len(undo) == 1 and undo[0].report["parse"]["ok"] is True

    shell._handle_repl_line("undo", ref, undo_stack=undo)

    assert undo == []
    assert _tree_bytes(project) == before


def test_cancelling_the_metric_also_takes_back_the_measure_made_for_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _shop(tmp_path)
    before = _tree_bytes(project)
    backend.set_backend(
        _Script({"Metric key": "tax_collected", "Create this metric?": False, **NEW_TAX_MEASURE})
    )
    undo: list[Any] = []

    shell._handle_repl_line(
        "author metric", PackageReference(source_path=str(project)), undo_stack=undo
    )

    assert "Authoring cancelled; no files changed." in capsys.readouterr().out
    assert undo == []
    assert _tree_bytes(project) == before


REVENUE_OFFSET = {"unit": "year", "value": 1}
GROWTH_AGGREGATE = {"kind": "aggregate", "measure": "measure.shop.revenue", "aggregation": "sum"}
GROWTH_PRIOR = {"kind": "prior_period", "input": GROWTH_AGGREGATE, "offset": REVENUE_OFFSET}


@pytest.mark.parametrize(
    ("recipe", "answers", "expected", "value_type"),
    [
        ("Running total", {}, {"kind": "cumulative"}, "currency"),
        (
            "Rolling window",
            {"Window unit": "Weeks", "Window length": "4"},
            {"kind": "rolling", "window": {"unit": "week", "value": 4}},
            "currency",
        ),
        (
            "Period to date",
            {"Period": "Quarter to date"},
            {"kind": "period_to_date", "period": "quarter"},
            "currency",
        ),
        (
            "Prior period",
            {},
            {"kind": "prior_period", "offset": {"unit": "month", "value": 1}},
            "currency",
        ),
        (
            "Growth",
            {"Compare with how far back": "Years"},
            {
                "kind": "derived",
                "expression": {
                    "kind": "binary",
                    "op": "divide",
                    "null_behavior": "null_if_zero",
                    "left": {
                        "kind": "binary",
                        "op": "subtract",
                        "left": GROWTH_AGGREGATE,
                        "right": GROWTH_PRIOR,
                    },
                    "right": GROWTH_PRIOR,
                },
            },
            "percent",
        ),
    ],
)
def test_every_recipe_over_time_writes_a_metric_that_parses_and_compiles(
    tmp_path: Path,
    recipe: str,
    answers: dict[str, Any],
    expected: dict[str, Any],
    value_type: str,
) -> None:
    from semantic_rails.runtime import Runtime

    project = _shop(tmp_path, calendar=True)

    _, metric = _author_metric(
        project,
        {"Metric key": "timed", "Metric recipe": recipe, "Measure": "revenue - ", **answers},
    )

    assert {key: metric[key] for key in expected} == expected
    if expected["kind"] != "derived":
        assert metric["measure"] == "measure.shop.revenue"
    assert metric["value_type"] == value_type
    assert metric["temporal_role"] == "temporal_role.shop_order_ordered_at"
    compiled = Runtime.from_path(str(project)).compile(
        {
            "version": 1,
            "select": [{"expression": {"metric": "metric.shop.timed"}}],
            # A rolling window is read at its own unit; a 4-week window has no month grain.
            "time": {
                "temporal_role": metric["temporal_role"],
                "grain": dict(metric.get("window", {})).get("unit", "month"),
            },
        }
    )
    assert compiled.get("ok", True) and "SELECT" in str(compiled.get("rendered_sql") or "")


def test_recipes_that_need_a_calendar_are_offered_only_with_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script, _ = _author_metric(
        _shop(tmp_path), {"Metric key": "total", "Measure to publish": "revenue - "}
    )

    assert [option.split(" - ")[0] for option in script.options["Metric recipe"]] == [
        "Aggregate",
        "Ratio",
        "Running total",
        "Period to date",
    ]
    assert "need a calendar table in the package" in capsys.readouterr().out


def test_a_recipe_over_time_needs_a_time_axis(tmp_path: Path) -> None:
    from semantic_rails.errors import SemanticLayerError

    project = _shop(tmp_path)
    backend.set_backend(
        _Script(
            {
                "Metric key": "customers_to_date",
                "Metric recipe": "Running total",
                "Measure": "customer_count - ",
            }
        )
    )

    with pytest.raises(SemanticLayerError, match="Running total needs a time axis"):
        shell._handle_repl_line(
            "author metric", PackageReference(source_path=str(project)), undo_stack=[]
        )
    assert not (project / "metrics" / "core" / "customers_to_date.yml").exists()


def test_changing_the_recipe_drops_the_old_recipes_fields(tmp_path: Path) -> None:
    project = _shop(tmp_path, calendar=True)
    _author_metric(
        project,
        {"Metric key": "revenue_7d", "Metric recipe": "Rolling window", "Measure": "revenue - "},
    )

    _, metric = _author_metric(
        project,
        {
            "Metric key": "revenue_7d",
            "Manage and update this existing metric?": True,
            "Metric recipe": "Aggregate",
            "Measure to publish": "revenue - ",
            "Update this metric?": True,
        },
    )

    assert metric["kind"] == "aggregate"
    assert "window" not in metric
