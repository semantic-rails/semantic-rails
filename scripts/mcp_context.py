"""Measure the query MCP's context cost and the planner's accuracy.

The runner drives the packaged query MCP in process through the JSON-RPC
dispatcher that ``semantic-rails mcp stdio`` uses
(:func:`semantic_rails.mcp_server.handle_jsonrpc_message`), so every measured
payload is what a stdio client receives. Each run builds its own fixture: a
temporary copy of the bundled ``jaffle_shop`` package plus its seed CSVs and
post-load SQL, from which the runtime builds a fresh DuckDB file. The checkout
is only read, never written.

Token counts are ``round(len(text) / 4)`` over the JSON a model sees: the
compact ``structuredContent`` (what Claude Code forwards) and the
``content[0].text`` channel (what text-forwarding hosts forward). The proxy is
deterministic and needs no tokenizer download; real tokenizers differ in
absolute terms, so compare runs of this script with each other, not with
provider bills.

Two gates read the files under ``tests/semantic_rails/mcp_context/``:

* ``budgets.json`` holds a ceiling for every measured size. A run fails when a
  gated metric exceeds its budget by more than the tolerance. Architect MCP
  sizes are tracked but not gated.
* ``eval_jaffle.jsonl`` is the frozen gold question set, and
  ``plan_accuracy_baseline.json`` records each case's planner outcome. A run
  fails when a case's outcome gets worse or a gold query's answer changes.

Usage::

    uv run python scripts/mcp_context.py               # report + gates
    uv run python scripts/mcp_context.py --markdown    # tables for a PR
    uv run python scripts/mcp_context.py --write-baseline

``--write-baseline`` rewrites both baseline files from the current run; review
the diff before committing it.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CONTEXT_DIR = REPO_ROOT / "tests" / "semantic_rails" / "mcp_context"
BUDGETS_PATH = CONTEXT_DIR / "budgets.json"
EVAL_SET_PATH = CONTEXT_DIR / "eval_jaffle.jsonl"
PLAN_BASELINE_PATH = CONTEXT_DIR / "plan_accuracy_baseline.json"
# Commitment to the held-out split: 12 more cases kept outside this repository
# so the planner can't be tuned against them. ``--eval-file`` checks a copy.
HELDOUT_SET_SHA256 = "ce5ef85b14f8b92a3f6944a55dd4657631ddde104006dcb50178fd0800027730"

PACKAGE_ID = "jaffle_shop"
DEFAULT_TOLERANCE = 0.02
# Budgets also allow this many tokens of slack, so small metrics (an empty
# instructions string, a short error) don't fail on a one-word change.
ABSOLUTE_SLACK_TOKENS = 8
# Gold queries run with an explicit row limit so a default MCP row cap can't
# truncate the reference answer.
GOLD_MAX_ROWS = 100_000


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def build_jaffle_fixture(root: Path) -> Path:
    """Copy the bundled jaffle_shop package and its seed inputs under ``root``.

    ``package.seed`` names ``data/jaffle_csv`` and ``data/seed_jaffle.sql``.
    With both copied next to the package, the runtime resolves them inside
    the fixture and builds ``data/jaffle_shop.duckdb`` there on first use.
    """

    package = root / PACKAGE_ID
    shutil.copytree(REPO_ROOT / "configs" / "semantic_rails" / PACKAGE_ID, package)
    data = package / "data"
    data.mkdir(exist_ok=True)
    shutil.copytree(REPO_ROOT / "data" / "jaffle_csv", data / "jaffle_csv")
    shutil.copy2(REPO_ROOT / "data" / "seed_jaffle.sql", data / "seed_jaffle.sql")
    return package


@contextmanager
def temporary_fixture() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="mcp-context-") as tmp:
        yield build_jaffle_fixture(Path(tmp))


# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------

_TIMING_RE = re.compile(r'("timing_ms"\s*:\s*)-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')


def approx_tokens(text: str) -> int:
    return round(len(text) / 4)


def normalize_volatile(text: str) -> str:
    """Pin run-to-run noise to a fixed width so sizes are reproducible.

    ``timing_ms`` is the only field whose width varies between identical runs;
    request ids are fixed-width hex.
    """

    return _TIMING_RE.sub(r"\g<1>10.000", text)


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def result_tokens(result: Mapping[str, Any]) -> tuple[int, int]:
    """Return (structured, text) tokens for one ``tools/call`` result."""

    text = "".join(
        str(block.get("text", ""))
        for block in result.get("content", []) or []
        if isinstance(block, Mapping) and block.get("type") == "text"
    )
    structured = result.get("structuredContent")
    rendered = compact_json(structured) if structured is not None else text
    return approx_tokens(normalize_volatile(rendered)), approx_tokens(normalize_volatile(text))


def tool_list_sizes(tools: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Size a ``tools/list`` result.

    ``model_visible`` counts what a model is shown (name, description, input
    schema); ``wire`` counts the full definitions, including output schemas
    and annotations.
    """

    visible = [
        approx_tokens(
            json.dumps(
                {
                    "name": tool.get("name", ""),
                    "description": tool.get("description") or "",
                    "input_schema": tool.get("inputSchema") or {},
                },
                sort_keys=True,
            )
        )
        for tool in tools
    ]
    return {
        "tools": len(tools),
        "model_visible_tokens": sum(visible),
        "largest_tool_tokens": max(visible, default=0),
        "wire_tokens": sum(approx_tokens(json.dumps(tool, sort_keys=True)) for tool in tools),
    }


# ---------------------------------------------------------------------------
# In-process JSON-RPC client
# ---------------------------------------------------------------------------


class QueryMCPClient:
    """JSON-RPC client for the query MCP's stdio dispatcher, run in process."""

    def __init__(self, package_path: Path) -> None:
        from semantic_rails.mcp import SemanticLayerMCPAdapter

        self.adapter = SemanticLayerMCPAdapter.from_path(str(package_path))
        self._next_id = 0

    def __enter__(self) -> QueryMCPClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.adapter.close()

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        from semantic_rails.mcp_server import handle_jsonrpc_message

        self._next_id += 1
        # Encode params as a client would, so no call shares objects with another.
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        response = handle_jsonrpc_message(self.adapter, json.loads(json.dumps(message)))
        if not isinstance(response, dict) or "result" not in response:
            raise RuntimeError(f"MCP {method} returned no result: {response!r}")
        # serve_stdio writes json.dumps(..., default=str); round-trip the same
        # way so dates and decimals take their wire form.
        result: dict[str, Any] = json.loads(json.dumps(response["result"], default=str))
        return result

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": dict(arguments)})

    def tool_payload(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        payload = self.call_tool(name, arguments).get("structuredContent")
        return dict(payload) if isinstance(payload, Mapping) else {}


# ---------------------------------------------------------------------------
# Scripted calls: three analytics questions, answered two ways
# ---------------------------------------------------------------------------

ORDER_TIME = "temporal_role.jaffle_order_time"
STORE = "dimension.jaffle_store_name"
PRODUCT = "dimension.jaffle_item_product_name"
REVENUE = {"measure": "measure.jaffle.revenue_usd", "aggregation": "sum"}
ITEM_REVENUE = {"measure": "measure.jaffle.item_revenue_usd", "aggregation": "sum"}
SEGMENT = "segment.jaffle.high_value_customers"


def _window(grain: str, start: str, end: str) -> dict[str, str]:
    return {"temporal_role": ORDER_TIME, "grain": grain, "start": start, "end": end}


QUESTIONS = (
    "monthly revenue by store for 2017",
    "top 5 products by revenue in 2017",
    "average order value by store in Q2 2017 for Philadelphia and Brooklyn",
)
# The queries an agent composes for the three questions after discovery.
Q1 = {
    "version": 2,
    "select": [{"as": "revenue_usd", "expression": REVENUE}],
    "time": _window("month", "2017-01-01", "2018-01-01"),
    "group_by": [STORE],
    "order_by": [{"field": "time", "direction": "ASC"}, {"field": STORE, "direction": "ASC"}],
}
Q2 = {
    "version": 2,
    "select": [{"as": "item_revenue_usd", "expression": ITEM_REVENUE}],
    "time": _window("year", "2017-01-01", "2018-01-01"),
    "group_by": [PRODUCT],
    "order_by": [{"field": "item_revenue_usd", "direction": "DESC"}],
    "limit": 5,
}
Q3 = {
    "version": 2,
    "select": [{"as": "aov_usd", "expression": {"metric": "metric.sales.aov_usd"}}],
    "time": _window("quarter", "2017-04-01", "2017-07-01"),
    "group_by": [STORE],
    "where": [{"field": STORE, "op": "in", "value": ["Philadelphia", "Brooklyn"]}],
    "order_by": [{"field": "aov_usd", "direction": "DESC"}],
}
# A plausible agent mistake: a time window with no grain groups by the raw
# timestamp and returns one row per distinct order time.
NO_GRAIN_WINDOW = {
    **Q3,
    "time": {"temporal_role": ORDER_TIME, "start": "2017-04-01", "end": "2017-07-01"},
}
MINIMAL_DISCOVER = {"verbosity": "minimal", "limit": 5}

Step = tuple[str, str, dict[str, Any]]

SESSIONS: dict[str, list[Step]] = {
    # The loop the tool descriptions prescribe, at every tool's default
    # verbosity and detail.
    "by_the_book": [
        ("s0_capabilities", "capabilities", {}),
        ("s1_catalog", "catalog", {}),
        ("q1_discover", "discover", {"terms": "monthly revenue by store"}),
        ("q1_inspect_measure", "inspect", {"object_id": REVENUE["measure"]}),
        ("q1_inspect_dimension", "inspect", {"object_id": STORE}),
        ("q1_plan", "plan", {"intent": QUESTIONS[0]}),
        ("q1_validate", "validate", {"query": Q1}),
        ("q1_compile", "compile", {"query": Q1}),
        ("q1_execute", "execute", {"query": Q1}),
        ("q2_discover", "discover", {"terms": "top products by revenue"}),
        ("q2_inspect_dimension", "inspect", {"object_id": PRODUCT}),
        ("q2_plan", "plan", {"intent": QUESTIONS[1]}),
        (
            "q2_build_options",
            "build-options",
            {"query": {"version": 2, "select": Q2["select"]}, "focus_terms": "product"},
        ),
        ("q2_validate", "validate", {"query": Q2}),
        ("q2_compile", "compile", {"query": Q2}),
        ("q2_execute", "execute", {"query": Q2}),
        ("q3_discover", "discover", {"terms": "average order value"}),
        ("q3_inspect_metric", "inspect", {"object_id": "metric.sales.aov_usd"}),
        ("q3_valid_values", "valid-values", {"dimension_id": STORE}),
        ("q3_plan", "plan", {"intent": QUESTIONS[2]}),
        ("q3_validate", "validate", {"query": Q3}),
        ("q3_compile", "compile", {"query": Q3}),
        ("q3_execute", "execute", {"query": Q3}),
    ],
    # The leanest path the current surface supports.
    "lean": [
        ("q1_discover", "discover", {"terms": "monthly revenue by store", **MINIMAL_DISCOVER}),
        ("q1_plan", "plan", {"intent": QUESTIONS[0], "detail": "query"}),
        ("q1_execute", "execute", {"query": Q1, "row_format": "columns"}),
        ("q2_discover", "discover", {"terms": "top products by revenue", **MINIMAL_DISCOVER}),
        ("q2_plan", "plan", {"intent": QUESTIONS[1], "detail": "query"}),
        ("q2_execute", "execute", {"query": Q2, "row_format": "columns"}),
        ("q3_discover", "discover", {"terms": "average order value", **MINIMAL_DISCOVER}),
        ("q3_plan", "plan", {"intent": QUESTIONS[2], "detail": "query"}),
        ("q3_execute", "execute", {"query": Q3, "row_format": "columns"}),
    ],
}

# One call per tool, passing only what a caller must supply. Claude Code warns
# about tool results over 10K tokens and spills results over 25K to a file.
DEFAULT_PROBES: list[Step] = [
    ("capabilities", "capabilities", {}),
    ("catalog", "catalog", {}),
    ("discover", "discover", {"terms": "revenue by store"}),
    ("inspect", "inspect", {"object_id": REVENUE["measure"]}),
    ("build_options", "build-options", {"query": {"version": 2, "select": Q1["select"]}}),
    ("valid_values", "valid-values", {"dimension_id": STORE}),
    ("plan", "plan", {"intent": QUESTIONS[0]}),
    ("validate", "validate", {"query": Q1}),
    ("compile", "compile", {"query": Q1}),
    ("execute", "execute", {"query": Q1}),
    ("execute_no_grain_window", "execute", {"query": NO_GRAIN_WINDOW}),
    ("segment_validate", "segment-validate", {"segment_id": SEGMENT}),
    ("segment_explain", "segment-explain", {"segment_id": SEGMENT}),
    ("segment_preview", "segment-preview", {"segment_id": SEGMENT}),
]

# Typical agent mistakes, each with whether the call should still succeed.
# Errors should be small and sent once.
ERROR_PROBES: list[tuple[str, str, dict[str, Any], bool]] = [
    ("inspect_label_not_id", "inspect", {"object_id": "revenue"}, False),
    (
        "validate_unknown_dimension",
        "validate",
        {"query": {**Q1, "group_by": ["dimension.jaffle_store"]}},
        False,
    ),
    (
        "validate_fanout",
        "validate",
        {
            "query": {
                **Q2,
                "select": Q1["select"],
                "order_by": [{"field": "revenue_usd", "direction": "DESC"}],
            }
        },
        False,
    ),
    ("discover_unknown_argument", "discover", {"term": "revenue"}, True),
]


class MeasurementError(RuntimeError):
    """A scripted call didn't behave as scripted, so its size means nothing."""


def _measured_call(
    client: QueryMCPClient, name: str, tool: str, arguments: dict[str, Any], *, ok: bool = True
) -> tuple[int, int]:
    """Call a tool and return its (structured, text) tokens.

    A call that fails when it should succeed (or the reverse) raises instead:
    a broken tool must not look like a smaller response.
    """

    result = client.call_tool(tool, arguments)
    payload = result.get("structuredContent")
    succeeded = (
        isinstance(payload, Mapping) and payload.get("ok") is True and not result.get("isError")
    )
    if succeeded != ok:
        errors = payload.get("errors") if isinstance(payload, Mapping) else None
        codes = [issue.get("code") for issue in errors or [] if isinstance(issue, Mapping)]
        raise MeasurementError(f"{name}: {tool} ok={succeeded}, expected ok={ok}; errors={codes}")
    return result_tokens(result)


def measure_query_mcp(package_path: Path) -> dict[str, int]:
    """Measure the query MCP's upfront surface, default responses and sessions."""

    metrics: dict[str, int] = {}
    with QueryMCPClient(package_path) as client:
        initialize = client.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "mcp-context", "version": "1"},
            },
        )
        metrics["query.instructions_tokens"] = approx_tokens(
            str(initialize.get("instructions") or "")
        )
        tools = client.request("tools/list")["tools"]
        metrics.update(
            {f"query.tools_list.{key}": value for key, value in tool_list_sizes(tools).items()}
        )
        resources = client.request("resources/list")["resources"]
        metrics["query.resources_list_tokens"] = approx_tokens(
            json.dumps(resources, sort_keys=True)
        )
        for resource in resources:
            body = client.request("resources/read", {"uri": resource["uri"]})
            text = "".join(str(item.get("text", "")) for item in body.get("contents", []))
            metrics[f"query.resource.{resource['name']}_tokens"] = approx_tokens(
                normalize_volatile(text)
            )
        prompts = client.request("prompts/list")["prompts"]
        metrics["query.prompts_list_tokens"] = approx_tokens(json.dumps(prompts, sort_keys=True))
        default_structured: list[int] = []
        default_text: list[int] = []
        for name, tool, arguments in DEFAULT_PROBES:
            structured, text_tokens = _measured_call(client, name, tool, arguments)
            metrics[f"query.default.{name}_tokens"] = structured
            default_structured.append(structured)
            default_text.append(text_tokens)
        metrics["query.default.max_structured_tokens"] = max(default_structured)
        metrics["query.default.max_text_tokens"] = max(default_text)
        for name, tool, arguments, ok in ERROR_PROBES:
            metrics[f"query.error.{name}_tokens"] = _measured_call(
                client, name, tool, arguments, ok=ok
            )[0]
    for session, steps in SESSIONS.items():
        # A fresh runtime per session, so one session's caches can't shrink
        # or grow another's responses.
        structured_total = text_total = largest = 0
        with QueryMCPClient(package_path) as client:
            for step, tool, arguments in steps:
                structured, text_tokens = _measured_call(
                    client, f"{session}.{step}", tool, arguments
                )
                structured_total += structured
                text_total += text_tokens
                largest = max(largest, structured)
        metrics[f"query.session.{session}.structured_tokens"] = structured_total
        metrics[f"query.session.{session}.text_tokens"] = text_total
        metrics[f"query.session.{session}.max_step_structured_tokens"] = largest
    return metrics


def measure_architect_mcp(workspace_root: Path) -> dict[str, int]:
    """Size the Architect MCP's tool list and instructions (tracked, not gated)."""

    from semantic_rails.architect_mcp import create_architect_mcp_server

    server = create_architect_mcp_server(workspace_root=workspace_root)
    tools = [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in asyncio.run(server.list_tools())
    ]
    metrics = {
        f"architect.tools_list.{key}": value for key, value in tool_list_sizes(tools).items()
    }
    metrics["architect.instructions_tokens"] = approx_tokens(str(server.instructions or ""))
    return metrics


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetCheck:
    metric: str
    value: int | None
    budget: int | None
    status: str  # ok | over | under | unbudgeted | unmeasured | tracked

    @property
    def failed(self) -> bool:
        return self.status in {"over", "unbudgeted", "unmeasured"}


def load_budgets(path: Path = BUDGETS_PATH) -> dict[str, Any]:
    budgets: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return budgets


def _slack(budget: int, tolerance: float) -> int:
    return max(ABSOLUTE_SLACK_TOKENS, math.ceil(budget * tolerance))


def check_budgets(
    metrics: Mapping[str, int], budgets: Mapping[str, Any], *, prefix: str
) -> list[BudgetCheck]:
    """Compare measured metrics under ``prefix`` with their budgets.

    Gated metrics fail when they exceed the budget plus slack, when a metric
    has no budget, or when a budgeted metric is no longer measured. A metric
    that drops below its budget minus slack is reported as ``under``: lower
    the budget with ``--write-baseline`` to lock the saving in.
    """

    tolerance = float(budgets.get("tolerance", DEFAULT_TOLERANCE))
    gated: Mapping[str, int] = budgets.get("gated", {})
    tracked: Mapping[str, int] = budgets.get("tracked", {})
    checks: list[BudgetCheck] = []
    names = sorted({*metrics, *gated, *tracked})
    for name in names:
        if not name.startswith(prefix):
            continue
        value = metrics.get(name)
        if name in tracked:
            checks.append(BudgetCheck(name, value, tracked[name], "tracked"))
            continue
        budget = gated.get(name)
        if budget is None:
            status = "unbudgeted"
        elif value is None:
            status = "unmeasured"
        elif value > budget + _slack(budget, tolerance):
            status = "over"
        elif value < budget - _slack(budget, tolerance):
            status = "under"
        else:
            status = "ok"
        checks.append(BudgetCheck(name, value, budget, status))
    return checks


# ---------------------------------------------------------------------------
# Frozen eval set and planner accuracy
# ---------------------------------------------------------------------------

PASS = "pass"
FLAGGED = "wrong_flagged"
SILENT = "wrong_silent"
OUTCOME_RANK = {SILENT: 0, FLAGGED: 1, PASS: 2}
PLAN_STATUSES = frozenset({"ok", "low_confidence", "unrealizable", "out_of_scope"})
REFUSAL_STATUSES = frozenset({"out_of_scope", "unrealizable"})
EVAL_CASE_KEYS = frozenset(
    {
        "id",
        "split",
        "category",
        "question",
        "expect",
        "ordered",
        "trend",
        "gold_query",
        "alternatives",
        "gold_result",
        "note",
    }
)


def load_eval_cases(path: Path = EVAL_SET_PATH) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        case = json.loads(line)
        unknown = sorted(set(case) - EVAL_CASE_KEYS)
        if unknown:
            raise ValueError(f"{path.name}:{line_number}: unknown keys {unknown}")
        if case.get("expect") not in {"answer", "refuse"}:
            raise ValueError(f"{path.name}:{line_number}: expect must be 'answer' or 'refuse'")
        if (case["expect"] == "answer") != bool(case.get("gold_query")):
            raise ValueError(
                f"{path.name}:{line_number}: answer cases, and only they, need gold_query"
            )
        cases.append(case)
    ids = [case["id"] for case in cases]
    duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise ValueError(f"{path.name}: duplicate case ids {duplicates}")
    return cases


def eval_set_digest(cases: Iterable[Mapping[str, Any]]) -> str:
    """SHA-256 over the canonical JSON of every case, one per line.

    Whitespace, key order and line endings don't change the digest; any change
    to a question, gold query, expectation or split does.
    """

    canonical = "\n".join(compact_json(case) for case in cases)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def measure_aggregations(package_path: Path) -> dict[str, str]:
    from semantic_rails.runtime import Runtime

    runtime = Runtime.from_path(str(package_path))
    try:
        return {measure.id: measure.default_aggregation for measure in runtime.config.measures}
    finally:
        runtime.close()


def _canonical_expression(expr: Any, aggregations: Mapping[str, str]) -> Any:
    if isinstance(expr, Mapping):
        out = {
            str(key): _canonical_expression(value, aggregations)
            for key, value in expr.items()
            if value not in ("", None, [], {})
        }
        if out.get("kind") == "aggregate":
            out["kind"] = "measure"
        if out.get("kind") == "measure" and "measure" in out and not out.get("aggregation"):
            out["aggregation"] = aggregations.get(str(out["measure"]), "")
        return out
    if isinstance(expr, list):
        return [_canonical_expression(item, aggregations) for item in expr]
    return expr


def _bound(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace(" ", "T")
    for suffix in ("T00:00:00", "T00:00"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _filter_parts(item: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    op = str(item.get("op") or "=").strip().upper()
    value = item.get("value")
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    op_class = {"=": "in", "IN": "in", "!=": "not_in", "<>": "not_in", "NOT IN": "not_in"}.get(
        op, op
    )
    return str(item.get("field")), op_class, sorted(compact_json(v) for v in values)


def query_slots(
    query: Mapping[str, Any], aggregations: Mapping[str, str], *, ordered: bool
) -> dict[str, Any]:
    """Reduce a Query IR to the slots a question constrains.

    Slots come from the engine's own normalization, so shorthand and
    canonical spellings of the same query compare equal. Grouping by a
    dimension that a filter pins to one value doesn't change the answer, so
    such dimensions are left out of ``group_by``. Order only matters for
    ranking questions.
    """

    from semantic_rails.ast import normalize_query

    normalized = normalize_query(copy.deepcopy(dict(query))).to_dict()
    aliases: dict[str, str] = {}
    select: list[str] = []
    for item in normalized["select"]:
        signature = compact_json(_canonical_expression(item["expression"], aggregations))
        aliases[str(item["as"])] = signature
        select.append(signature)
    filters = [_filter_parts(item) for item in normalized["where"]]
    pinned = {field for field, op_class, values in filters if op_class == "in" and len(values) == 1}
    time = normalized.get("time") or {}
    start, end = _bound(time.get("start")), _bound(time.get("end"))
    slots: dict[str, Any] = {
        "select": sorted(select),
        "group_by": sorted(set(normalized["group_by"]) - pinned),
        "time_role": time.get("temporal_role") or None,
        "grain": time.get("grain") or None,
        "window": [start, end] if start or end else None,
        "where": sorted(compact_json(parts) for parts in filters),
        "metric_filters": sorted(
            compact_json(_canonical_expression(item, aggregations))
            for item in normalized["metric_filters"]
        ),
        "limit": normalized.get("limit"),
    }
    if ordered:
        order_by = normalized.get("order_by") or []
        first = order_by[0] if order_by else None
        slots["order"] = (
            None
            if first is None
            else [
                aliases.get(str(first["field"]), str(first["field"])),
                str(first["direction"]).upper(),
            ]
        )
    return slots


def mismatched_slots(
    case: Mapping[str, Any], query: Mapping[str, Any], aggregations: Mapping[str, str]
) -> list[str]:
    """Slots where ``query`` differs from the gold query and every alternative."""

    ordered = bool(case.get("ordered"))
    try:
        actual = query_slots(query, aggregations, ordered=ordered)
    except Exception:  # noqa: BLE001 - an unparseable plan is a wrong plan
        return ["invalid_query"]
    candidates = [case["gold_query"], *(case.get("alternatives") or [])]
    best: list[str] | None = None
    for candidate in candidates:
        expected = query_slots(candidate, aggregations, ordered=ordered)
        diff = [slot for slot, value in expected.items() if actual.get(slot) != value]
        if not diff:
            return []
        if best is None:
            best = diff
    return best or []


@dataclass(frozen=True)
class PlanOutcome:
    case_id: str
    category: str
    outcome: str
    status: str
    warning_codes: tuple[str, ...]
    mismatched: tuple[str, ...]


class EvaluationError(RuntimeError):
    """``plan`` failed outright, so its answer can't be graded."""


def score_plan_response(
    case: Mapping[str, Any], response: Mapping[str, Any], aggregations: Mapping[str, str]
) -> PlanOutcome:
    """Grade one ``plan(detail="query")`` response against its gold case.

    ``pass`` means the plan matched the gold slots, or refused an unanswerable
    question as ``out_of_scope`` or ``unrealizable``. A wrong plan is
    ``wrong_flagged`` when the response signals doubt (a non-``ok`` status or
    any warning) and ``wrong_silent`` when it reports ``ok`` with no warnings.
    A failed call (an error envelope, or no recognizable status) raises
    ``EvaluationError`` rather than scoring as a refusal or a flagged answer.
    """

    status = str(response.get("status") or "")
    if response.get("ok") is not True or status not in PLAN_STATUSES:
        errors = response.get("errors") or []
        codes = [issue.get("code") for issue in errors if isinstance(issue, Mapping)]
        raise EvaluationError(f"{case['id']}: plan failed (status={status!r}, errors={codes})")
    warnings = tuple(
        str(item.get("code", ""))
        for item in response.get("warnings") or []
        if isinstance(item, Mapping)
    )
    loud = status != "ok" or bool(warnings)
    wrong = FLAGGED if loud else SILENT
    mismatched: tuple[str, ...]
    if case["expect"] == "refuse":
        refused = status in REFUSAL_STATUSES
        outcome, mismatched = (PASS, ()) if refused else (wrong, ("answered",))
    else:
        query = (response.get("best") or {}).get("query_ir")
        if status in REFUSAL_STATUSES or not isinstance(query, Mapping):
            outcome, mismatched = wrong, ("refused",)
        else:
            diff = tuple(mismatched_slots(case, query, aggregations))
            outcome, mismatched = (PASS, ()) if not diff else (wrong, diff)
    return PlanOutcome(
        str(case["id"]), str(case.get("category", "")), outcome, status, warnings, mismatched
    )


def run_plan_accuracy(package_path: Path, cases: Sequence[Mapping[str, Any]]) -> list[PlanOutcome]:
    aggregations = measure_aggregations(package_path)
    with QueryMCPClient(package_path) as client:
        return [
            score_plan_response(
                case,
                client.tool_payload("plan", {"intent": case["question"], "detail": "query"}),
                aggregations,
            )
            for case in cases
        ]


def plan_summary(outcomes: Sequence[PlanOutcome]) -> dict[str, int]:
    counts = Counter(outcome.outcome for outcome in outcomes)
    return {"cases": len(outcomes), **{key: counts.get(key, 0) for key in (PASS, FLAGGED, SILENT)}}


def plan_regressions(
    outcomes: Sequence[PlanOutcome], baseline: Mapping[str, Any]
) -> tuple[list[str], list[str]]:
    """Return (regressions, improvements) against the recorded baseline."""

    recorded: Mapping[str, str] = baseline.get("cases", {})
    regressions: list[str] = []
    improvements: list[str] = []
    for outcome in outcomes:
        before = recorded.get(outcome.case_id)
        if before is None:
            regressions.append(f"{outcome.case_id}: no baseline outcome recorded")
            continue
        if OUTCOME_RANK[outcome.outcome] < OUTCOME_RANK[before]:
            regressions.append(
                f"{outcome.case_id}: {before} -> {outcome.outcome} "
                f"(status={outcome.status}, mismatched={list(outcome.mismatched)})"
            )
        elif OUTCOME_RANK[outcome.outcome] > OUTCOME_RANK[before]:
            improvements.append(f"{outcome.case_id}: {before} -> {outcome.outcome}")
    missing = sorted(set(recorded) - {outcome.case_id for outcome in outcomes})
    regressions.extend(f"{case_id}: case no longer evaluated" for case_id in missing)
    return regressions, improvements


# ---------------------------------------------------------------------------
# Gold answers
# ---------------------------------------------------------------------------


_MIDNIGHT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]00:00:00$")
# Relative tolerance for numeric answers. Warehouse sums of doubles differ in
# the last bits between runs, and cent-valued data sits exactly on rounding
# boundaries, so answers are compared with a tolerance, never by hash.
ANSWER_REL_TOL = 1e-6
TIME_COLUMN = "time"


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        value = value.isoformat()
    # Some expressions return bucket dates, others midnight timestamps.
    return _MIDNIGHT_RE.sub(r"\1", str(value))


def _sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, value)
    if isinstance(value, float):
        return (2, value)
    return (3, str(value))


def _value_column(expression: Any, aggregations: Mapping[str, str]) -> str:
    """Name a select column by what it computes, not by its alias."""

    canonical = _canonical_expression(expression, aggregations)
    if canonical.get("kind") == "measure" and set(canonical) <= {"kind", "measure", "aggregation"}:
        return f"{canonical['measure']}:{canonical.get('aggregation', '')}"
    if canonical.get("kind") == "metric" and set(canonical) == {"kind", "metric"}:
        return str(canonical["metric"])
    return compact_json(canonical)


def _is_value_column(name: str) -> bool:
    return name != TIME_COLUMN and not name.startswith("dimension.")


def answer_table(
    rows: Sequence[Mapping[str, Any]],
    query: Mapping[str, Any],
    aggregations: Mapping[str, str],
    *,
    trend: bool,
) -> dict[str, Any]:
    """Reduce a result set to what answer equivalence compares.

    Each column is named by what it holds: a select column by its canonical
    expression, a grouping column by its dimension id, and the time bucket by
    ``"time"``, kept only when the question asks for a trend. Aliases and
    column order therefore don't matter, but which value sits in which column
    does.
    """

    from semantic_rails.ast import normalize_query

    normalized = normalize_query(copy.deepcopy(dict(query))).to_dict()
    select = {
        str(item["as"]): _value_column(item["expression"], aggregations)
        for item in normalized["select"]
    }
    named: dict[str, str] = {}
    for key in dict.fromkeys(str(key) for row in rows for key in row):
        if key.startswith("temporal_role."):
            if trend:
                named[key] = TIME_COLUMN
        else:
            named[key] = select.get(key, key)
    order = sorted(named, key=named.__getitem__)
    return {
        "columns": [named[key] for key in order],
        "rows": [[_canonical_value(row.get(key)) for key in order] for row in rows],
    }


def _column_alignment(expected: Sequence[str], actual: Sequence[str]) -> list[int] | None:
    """Position in ``actual`` of each expected column, or None if they can't align.

    Columns align by name. Two formulations of the same answer can compute
    one value differently (a dedicated count measure vs a filtered count);
    when a single value column is all that differs, those two align.
    """

    if len(expected) != len(actual):
        return None
    unused = list(range(len(actual)))
    alignment: list[int | None] = []
    for name in expected:
        match = next((index for index in unused if actual[index] == name), None)
        if match is not None:
            unused.remove(match)
        alignment.append(match)
    missing = [position for position, match in enumerate(alignment) if match is None]
    if not missing:
        return [match for match in alignment if match is not None]
    if (
        len(missing) == 1
        and _is_value_column(expected[missing[0]])
        and _is_value_column(actual[unused[0]])
    ):
        alignment[missing[0]] = unused[0]
        return [match for match in alignment if match is not None]
    return None


def _same_value(expected: Any, actual: Any) -> bool:
    if isinstance(expected, float) and isinstance(actual, float):
        return math.isclose(expected, actual, rel_tol=ANSWER_REL_TOL, abs_tol=1e-9)
    return type(expected) is type(actual) and expected == actual


def _same_row(expected: Sequence[Any], actual: Sequence[Any]) -> bool:
    return len(expected) == len(actual) and all(map(_same_value, expected, actual))


def answers_match(expected: Mapping[str, Any], actual: Mapping[str, Any], *, ordered: bool) -> bool:
    """Answer equivalence: same columns and rows, numbers within ``ANSWER_REL_TOL``.

    Row order matters only for ranking questions.
    """

    alignment = _column_alignment(expected["columns"], actual["columns"])
    if alignment is None or len(expected["rows"]) != len(actual["rows"]):
        return False
    rows = [[row[index] for index in alignment] for row in actual["rows"]]
    if ordered:
        return all(map(_same_row, expected["rows"], rows))
    remaining = list(rows)
    for row in expected["rows"]:
        index = next(
            (i for i, candidate in enumerate(remaining) if _same_row(row, candidate)), None
        )
        if index is None:
            return False
        remaining.pop(index)
    return True


def gold_answer(
    client: QueryMCPClient,
    query: Mapping[str, Any],
    case: Mapping[str, Any],
    aggregations: Mapping[str, str],
) -> dict[str, Any]:
    payload = client.tool_payload(
        "execute", {"query": {**dict(query), "limits": {"max_rows": GOLD_MAX_ROWS}}}
    )
    if not payload.get("ok"):
        codes = [issue.get("code") for issue in payload.get("errors") or []]
        raise ValueError(f"{case['id']}: gold query failed: {codes}")
    if payload.get("truncated"):
        raise ValueError(f"{case['id']}: gold answer was truncated")
    return answer_table(
        payload.get("rows") or [], query, aggregations, trend=bool(case.get("trend"))
    )


def check_gold_answers(package_path: Path, cases: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return problems: gold queries that fail, change answers, or disagree with alternatives."""

    aggregations = measure_aggregations(package_path)
    problems: list[str] = []
    with QueryMCPClient(package_path) as client:
        for case in cases:
            if case["expect"] != "answer":
                continue
            ordered = bool(case.get("ordered"))
            try:
                actual = gold_answer(client, case["gold_query"], case, aggregations)
                frozen = case.get("gold_result")
                if not actual["rows"]:
                    problems.append(f"{case['id']}: gold answer is empty")
                if not isinstance(frozen, Mapping) or not answers_match(
                    frozen, actual, ordered=ordered
                ):
                    problems.append(f"{case['id']}: gold answer no longer matches the frozen one")
                for index, alternative in enumerate(case.get("alternatives") or []):
                    answer = gold_answer(client, alternative, case, aggregations)
                    if not answers_match(actual, answer, ordered=ordered):
                        problems.append(f"{case['id']}: alternative {index} answers differently")
            except ValueError as exc:
                problems.append(str(exc))
    return problems


def _stored_value(value: Any) -> Any:
    return float(f"{value:.10g}") if isinstance(value, float) else value


def fill_gold_results(path: Path, package_path: Path) -> None:
    """Authoring helper: write each case's ``gold_result`` into ``path``."""

    aggregations = measure_aggregations(package_path)
    cases = load_eval_cases(path)
    with QueryMCPClient(package_path) as client:
        for case in cases:
            if case["expect"] != "answer":
                continue
            answer = gold_answer(client, case["gold_query"], case, aggregations)
            rows = [[_stored_value(value) for value in row] for row in answer["rows"]]
            if not case.get("ordered"):
                rows.sort(key=lambda row: [_sort_key(value) for value in row])
            case["gold_result"] = {"columns": answer["columns"], "rows": rows}
    path.write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")


# ---------------------------------------------------------------------------
# Report and CLI
# ---------------------------------------------------------------------------


def write_baseline(
    metrics: Mapping[str, int], outcomes: Sequence[PlanOutcome], cases: Sequence[Mapping[str, Any]]
) -> None:
    budgets = load_budgets() if BUDGETS_PATH.exists() else {}
    tolerance = float(budgets.get("tolerance", DEFAULT_TOLERANCE))
    document = {
        "description": (
            "Ceilings for scripts/mcp_context.py measurements, in tokens = round(chars / 4). "
            "Gated metrics fail CI above budget + max(8, budget * tolerance); tracked ones are "
            "reported only. Regenerate with --write-baseline and review the diff."
        ),
        "tolerance": tolerance,
        "gated": {
            name: value for name, value in sorted(metrics.items()) if name.startswith("query.")
        },
        "tracked": {
            name: value for name, value in sorted(metrics.items()) if name.startswith("architect.")
        },
    }
    BUDGETS_PATH.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    baseline = {
        "eval_set_sha256": eval_set_digest(cases),
        "summary": plan_summary(outcomes),
        "cases": {outcome.case_id: outcome.outcome for outcome in outcomes},
    }
    PLAN_BASELINE_PATH.write_text(json.dumps(baseline, indent=1) + "\n", encoding="utf-8")


def _markdown_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def render_report(
    checks: Sequence[BudgetCheck], outcomes: Sequence[PlanOutcome], *, markdown: bool
) -> str:
    budget_rows = [[check.metric, check.value, check.budget, check.status] for check in checks]
    summary = plan_summary(outcomes)
    by_category: dict[str, Counter[str]] = {}
    for outcome in outcomes:
        by_category.setdefault(outcome.category, Counter())[outcome.outcome] += 1
    category_rows = [
        [category, sum(counts.values()), counts[PASS], counts[FLAGGED], counts[SILENT]]
        for category, counts in sorted(by_category.items())
    ]
    case_rows = [
        [
            o.case_id,
            o.category,
            o.outcome,
            o.status,
            ",".join(o.mismatched),
            ",".join(o.warning_codes),
        ]
        for o in outcomes
    ]
    if not markdown:
        lines = [f"{row[0]:<58} {row[1]!s:>9} {row[2]!s:>9}  {row[3]}" for row in budget_rows]
        lines.append(
            f"plan: {summary[PASS]}/{summary['cases']} pass, {summary[FLAGGED]} wrong but flagged, "
            f"{summary[SILENT]} wrong and silent"
        )
        lines.extend(" ".join(str(cell) for cell in row) for row in case_rows)
        return "\n".join(lines)
    return "\n\n".join(
        [
            "### Context budgets (tokens = chars/4)",
            _markdown_table(["metric", "measured", "budget", "status"], budget_rows),
            "### Planner accuracy (plan detail=query)",
            _markdown_table(
                ["category", "cases", "pass", "wrong, flagged", "wrong, silent"], category_rows
            ),
            _markdown_table(
                ["case", "category", "outcome", "status", "mismatched", "warnings"], case_rows
            ),
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--markdown", action="store_true", help="Print GitHub-flavored tables.")
    parser.add_argument(
        "--write-baseline", action="store_true", help="Rewrite budgets and plan baseline."
    )
    parser.add_argument(
        "--eval-file",
        type=Path,
        help="Score another eval file (for example a held-out split); prints aggregates only.",
    )
    parser.add_argument(
        "--fill-gold-results",
        type=Path,
        metavar="EVAL_FILE",
        help="Authoring helper: compute gold_result for every case in EVAL_FILE, then exit.",
    )
    args = parser.parse_args(argv)

    with temporary_fixture() as package:
        if args.fill_gold_results:
            fill_gold_results(args.fill_gold_results, package)
            return 0
        if args.eval_file:
            cases = load_eval_cases(args.eval_file)
            problems = check_gold_answers(package, cases)
            summary = plan_summary(run_plan_accuracy(package, cases))
            digest = eval_set_digest(cases)
            heldout = any(case.get("split") == "heldout" for case in cases)
            print(
                json.dumps(
                    {
                        "eval_set_sha256": digest,
                        "matches_heldout_commitment": digest == HELDOUT_SET_SHA256,
                        "plan": summary,
                        "gold_problems": len(problems),
                    },
                    indent=1,
                )
            )
            # A held-out copy that doesn't match the commitment isn't the frozen split.
            return 1 if problems or (heldout and digest != HELDOUT_SET_SHA256) else 0
        cases = load_eval_cases()
        metrics = measure_query_mcp(package)
        with tempfile.TemporaryDirectory(prefix="mcp-context-architect-") as workspace:
            metrics.update(measure_architect_mcp(Path(workspace)))
        outcomes = run_plan_accuracy(package, cases)
        gold_problems = check_gold_answers(package, cases)

    if args.write_baseline:
        write_baseline(metrics, outcomes, cases)
        print(
            f"wrote {BUDGETS_PATH.relative_to(REPO_ROOT)} and {PLAN_BASELINE_PATH.relative_to(REPO_ROOT)}"
        )
        return 0

    budgets = load_budgets()
    checks = check_budgets(metrics, budgets, prefix="")
    regressions, improvements = plan_regressions(
        outcomes, json.loads(PLAN_BASELINE_PATH.read_text())
    )
    print(render_report(checks, outcomes, markdown=args.markdown))
    failures = [
        f"budget {c.metric}: {c.value} vs {c.budget} ({c.status})" for c in checks if c.failed
    ]
    failures += [f"plan regression {item}" for item in regressions]
    failures += [f"gold {item}" for item in gold_problems]
    for item in improvements:
        print(f"improved (run --write-baseline to lock in): {item}")
    for check in checks:
        if check.status == "under":
            print(f"under budget (run --write-baseline to lock in): {check.metric}")
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
