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
  ``plan_accuracy_baseline.json`` records each case's planner outcome and the
  slots it gets wrong. A run fails when a case's outcome gets worse, when a
  wrong case gets another slot wrong, or when a gold query's answer changes.

Usage::

    uv run python scripts/mcp_context.py               # report + gates
    uv run python scripts/mcp_context.py --markdown    # tables for a PR
    uv run python scripts/mcp_context.py --write-baseline
    uv run python scripts/mcp_context.py --eval-file PATH

``--write-baseline`` rewrites both baseline files from the current run; review
the diff before committing it. ``--eval-file`` scores a copy of a frozen split
(such as the held-out one) and prints aggregates only.
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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CONTEXT_DIR = REPO_ROOT / "tests" / "semantic_rails" / "mcp_context"
BUDGETS_PATH = CONTEXT_DIR / "budgets.json"
EVAL_SET_PATH = CONTEXT_DIR / "eval_jaffle.jsonl"
PLAN_BASELINE_PATH = CONTEXT_DIR / "plan_accuracy_baseline.json"
# Digest of the frozen dev split, EVAL_SET_PATH. Change a case only through a
# reviewed revision of the eval set, and update this digest in that change.
DEV_SET_SHA256 = "9b5b8693dac9ab61f85c3496edd1785f024e3cfed35a0b7183e27cc19a640d0a"
# Commitment to the held-out split: 12 more cases kept outside this repository
# so the planner can't be tuned against them. ``--eval-file`` checks a copy.
HELDOUT_SET_SHA256 = "ce5ef85b14f8b92a3f6944a55dd4657631ddde104006dcb50178fd0800027730"

PACKAGE_ID = "jaffle_shop"
DEFAULT_TOLERANCE = 0.02
# Token budgets also allow this many tokens of slack, so small metrics (an
# empty instructions string, a short error) don't fail on a one-word change.
# Counts, such as the number of tools, get no slack.
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

_TIMING_RE = re.compile(r'("[A-Za-z0-9_]*_ms"\s*:\s*)-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')


def approx_tokens(text: str) -> int:
    return round(len(text) / 4)


def normalize_volatile(text: str) -> str:
    """Pin run-to-run noise to a fixed width so sizes are reproducible.

    Timings (``timing_ms``, ``compile_ms`` and any other ``*_ms`` field) are
    the only values whose width varies between identical runs; request ids
    are fixed-width hex.
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

# Typical agent mistakes, each with whether the call should still succeed and
# the code it should report. Errors should be small and sent once.
ERROR_PROBES: list[tuple[str, str, dict[str, Any], bool, str]] = [
    ("inspect_label_not_id", "inspect", {"object_id": "revenue"}, False, "OBJECT_NOT_FOUND"),
    (
        "validate_unknown_dimension",
        "validate",
        {
            "query": {
                **Q1,
                "group_by": ["dimension.jaffle_store"],
                "order_by": [{"field": "time", "direction": "ASC"}],
            }
        },
        False,
        "OBJECT_NOT_FOUND",
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
        "MIXED_GRAIN_INVALID",
    ),
    # A misspelled argument is ignored with a warning, not an error.
    ("discover_unknown_argument", "discover", {"term": "revenue"}, True, "DISCOVER_UNKNOWN_ARG"),
]


class MeasurementError(RuntimeError):
    """A scripted call didn't behave as scripted, so its size means nothing."""


_ID_PREFIXES = ("measure.", "metric.", "dimension.", "temporal_role.", "segment.", "entity.")
_QUERY_TOOLS = frozenset({"validate", "compile", "execute"})


def semantic_ids(node: Any) -> set[str]:
    """Every semantic object id mentioned anywhere in ``node``."""

    found: set[str] = set()
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, str) and item.startswith(_ID_PREFIXES):
            found.add(item)
    return found


def _measured_call(
    client: QueryMCPClient,
    name: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    ok: bool = True,
    code: str = "",
    surfaced: set[str] | None = None,
) -> tuple[int, int]:
    """Call a tool and return its (structured, text) tokens.

    A call that fails when it should succeed (or the reverse), or that
    doesn't report the expected code, raises instead: a broken tool must not
    look like a smaller response. With ``surfaced``, a query may only use ids
    that earlier calls returned, so a scripted session measures a path an
    agent could actually follow.
    """

    result = client.call_tool(tool, arguments)
    payload = result.get("structuredContent")
    payload = payload if isinstance(payload, Mapping) else {}
    succeeded = payload.get("ok") is True and not result.get("isError")
    issues = payload.get("errors" if not ok else "warnings") or []
    codes = [issue.get("code") for issue in issues if isinstance(issue, Mapping)]
    if succeeded != ok or (code and code not in codes):
        raise MeasurementError(
            f"{name}: {tool} ok={succeeded}, expected ok={ok}"
            + (f" with {code}" if code else "")
            + f"; reported {codes}"
        )
    if surfaced is not None:
        if tool in _QUERY_TOOLS:
            unseen = sorted(semantic_ids(arguments.get("query")) - surfaced)
            if unseen:
                raise MeasurementError(
                    f"{name}: {tool} uses ids no earlier call in the session surfaced: {unseen}"
                )
        else:
            surfaced.update(semantic_ids(payload))
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
        for name, tool, arguments, ok, code in ERROR_PROBES:
            metrics[f"query.error.{name}_tokens"] = _measured_call(
                client, name, tool, arguments, ok=ok, code=code
            )[0]
    for session, steps in SESSIONS.items():
        # A fresh runtime per session, so one session's caches can't shrink
        # or grow another's responses.
        structured_total = text_total = largest = 0
        surfaced: set[str] = set()
        with QueryMCPClient(package_path) as client:
            for step, tool, arguments in steps:
                structured, text_tokens = _measured_call(
                    client, f"{session}.{step}", tool, arguments, surfaced=surfaced
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


def _slack(metric: str, budget: int, tolerance: float) -> int:
    if not metric.endswith("_tokens"):
        return 0
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
        elif value > budget + _slack(name, budget, tolerance):
            status = "over"
        elif value < budget - _slack(name, budget, tolerance):
            status = "under"
        else:
            status = "ok"
        checks.append(BudgetCheck(name, value, budget, status))
    return checks


def rebaseline(
    metrics: Mapping[str, int], previous: Mapping[str, int], tolerance: float
) -> dict[str, int]:
    """New budgets for ``metrics``: an old budget stays while the value is within its slack.

    Only real changes move a budget, so rebaselining after an unrelated
    change doesn't churn every noisy metric.
    """

    budgets: dict[str, int] = {}
    for name, value in sorted(metrics.items()):
        budget = previous.get(name)
        within = budget is not None and abs(value - budget) <= _slack(name, budget, tolerance)
        budgets[name] = budget if within and budget is not None else value
    return budgets


# ---------------------------------------------------------------------------
# Frozen eval set and planner accuracy
# ---------------------------------------------------------------------------

# Planner outcomes, best first. A correct draft that the response flags anyway
# (a non-ok status or a warning) is a false alarm; a wrong one is caught by its
# status, only by a warning, or not at all.
PASS = "pass"
PASS_FLAGGED = "pass_flagged"
FLAGGED = "wrong_flagged"
WARNED = "wrong_warned"
SILENT = "wrong_silent"
OUTCOMES = (PASS, PASS_FLAGGED, FLAGGED, WARNED, SILENT)
OUTCOME_RANK = {outcome: len(OUTCOMES) - index for index, outcome in enumerate(OUTCOMES)}
OUTCOME_LABELS = {
    PASS: "pass",
    PASS_FLAGGED: "pass but flagged",
    FLAGGED: "wrong but flagged",
    WARNED: "wrong but warned",
    SILENT: "wrong and silent",
}
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
    op = " ".join(str(item.get("op") or "=").upper().split())
    value = item.get("value")
    if isinstance(value, (list, tuple)):
        # "=" with a list compares against the list's text, not its members.
        values = list(value)
        op_class = {"IN": "in", "NOT IN": "not_in"}.get(op, op)
    else:
        values = [value]
        op_class = {"=": "in", "IN": "in", "!=": "not_in", "<>": "not_in", "NOT IN": "not_in"}.get(
            op, op
        )
    return str(item.get("field")), op_class, sorted(compact_json(v) for v in values)


def _pinned_dimensions(normalized: Mapping[str, Any]) -> set[str]:
    """Dimensions a filter pins to a single value.

    Grouping by one doesn't change the answer: it adds a constant column.
    """

    return {
        field
        for field, op_class, values in map(_filter_parts, normalized["where"])
        if op_class == "in" and len(values) == 1
    }


def query_slots(
    query: Mapping[str, Any], aggregations: Mapping[str, str], *, ordered: bool
) -> dict[str, Any]:
    """Reduce a Query IR to the slots a question constrains.

    Slots come from the engine's own normalization, so shorthand and
    canonical spellings of the same query compare equal. Every field that can
    change the rows is a slot. Grouping by a dimension that a filter pins to
    one value doesn't change the answer, so such dimensions are left out of
    ``group_by``. Order only matters for ranking questions.
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
    time = normalized.get("time") or {}
    start, end = _bound(time.get("start")), _bound(time.get("end"))
    slots: dict[str, Any] = {
        "select": sorted(select),
        "group_by": sorted(set(normalized["group_by"]) - _pinned_dimensions(normalized)),
        "time_role": time.get("temporal_role") or None,
        "grain": time.get("grain") or None,
        "window": [start, end] if start or end else None,
        "fill": bool(time.get("fill")),
        "calendar_id": time.get("calendar_id") or "default",
        "temporal_role_overrides": normalized["temporal_role_overrides"],
        "path_policy": normalized["path_policy"],
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
    """Slots where ``query`` differs from the closest of the gold query and its alternatives."""

    ordered = bool(case.get("ordered"))
    try:
        actual = query_slots(query, aggregations, ordered=ordered)
    except Exception:  # noqa: BLE001 - an unparseable plan is a wrong plan
        return ["invalid_query"]
    candidates = [case["gold_query"], *(case.get("alternatives") or [])]
    diffs = []
    for candidate in candidates:
        expected = query_slots(candidate, aggregations, ordered=ordered)
        diffs.append([slot for slot, value in expected.items() if actual.get(slot) != value])
    return min(diffs, key=len)


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


# Runs a drafted query; returns its answer table, or None when it doesn't run in full.
AnswerOf = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]
# Mismatches that say nothing about individual slots: the plan drafted no
# query, or one the engine can't parse.
WHOLE_QUERY_MISMATCHES = frozenset({"refused", "invalid_query"})


def _returns_frozen_answer(case: Mapping[str, Any], answer: Mapping[str, Any] | None) -> bool:
    frozen = case.get("gold_result")
    return (
        isinstance(frozen, Mapping)
        and answer is not None
        and answers_match(frozen, answer, ordered=bool(case.get("ordered")))
    )


def score_plan_response(
    case: Mapping[str, Any],
    response: Mapping[str, Any],
    aggregations: Mapping[str, str],
    *,
    answer_of: AnswerOf,
) -> PlanOutcome:
    """Grade one ``plan(detail="query")`` response against its gold case.

    A plan is correct when it matched the gold slots and its query, run
    through ``answer_of``, returns the frozen answer, or when it refused an
    unanswerable question as ``out_of_scope`` or ``unrealizable``. A plan
    whose slots match but whose rows don't is wrong in slot ``answer``.

    A correct plan is ``pass``, or ``pass_flagged`` when the response still
    reports a non-``ok`` status or a warning (a false alarm). A wrong plan is
    ``wrong_flagged`` when its status isn't ``ok``, ``wrong_warned`` when the
    status is ``ok`` but a warning signals doubt, and ``wrong_silent``
    otherwise. A failed call (an error envelope, or no recognizable status)
    raises ``EvaluationError`` rather than being graded.
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
    wrong = FLAGGED if status != "ok" else WARNED if warnings else SILENT
    right = PASS_FLAGGED if status != "ok" or warnings else PASS
    mismatched: tuple[str, ...]
    if case["expect"] == "refuse":
        # A refusal is the right answer here, whatever else it reports.
        refused = status in REFUSAL_STATUSES
        outcome, mismatched = (PASS, ()) if refused else (wrong, ("answered",))
    else:
        query = (response.get("best") or {}).get("query_ir")
        if status in REFUSAL_STATUSES or not isinstance(query, Mapping):
            outcome, mismatched = wrong, ("refused",)
        else:
            diff = tuple(mismatched_slots(case, query, aggregations))
            if not diff and not _returns_frozen_answer(case, answer_of(query)):
                diff = ("answer",)
            outcome, mismatched = (right, ()) if not diff else (wrong, diff)
    return PlanOutcome(
        str(case["id"]), str(case.get("category", "")), outcome, status, warnings, mismatched
    )


def _drafted_answer(
    client: QueryMCPClient,
    query: Mapping[str, Any],
    *,
    case: Mapping[str, Any],
    aggregations: Mapping[str, str],
) -> dict[str, Any] | None:
    try:
        return query_answer(client, query, case, aggregations)
    except ValueError:
        return None


def run_plan_accuracy(package_path: Path, cases: Sequence[Mapping[str, Any]]) -> list[PlanOutcome]:
    aggregations = measure_aggregations(package_path)
    with QueryMCPClient(package_path) as client:
        return [
            score_plan_response(
                case,
                client.tool_payload("plan", {"intent": case["question"], "detail": "query"}),
                aggregations,
                answer_of=partial(_drafted_answer, client, case=case, aggregations=aggregations),
            )
            for case in cases
        ]


def plan_summary(outcomes: Sequence[PlanOutcome]) -> dict[str, int]:
    counts = Counter(outcome.outcome for outcome in outcomes)
    return {"cases": len(outcomes), **{key: counts.get(key, 0) for key in OUTCOMES}}


def plan_regressions(
    outcomes: Sequence[PlanOutcome], baseline: Mapping[str, Any]
) -> tuple[list[str], list[str]]:
    """Return (regressions, improvements) against the recorded baseline.

    A case regresses when its outcome gets worse, or when it gets a slot
    wrong that it used to get right, even if it was already wrong.
    """

    recorded: Mapping[str, Mapping[str, Any]] = baseline.get("cases", {})
    regressions: list[str] = []
    improvements: list[str] = []
    for outcome in outcomes:
        entry = recorded.get(outcome.case_id)
        if entry is None:
            regressions.append(f"{outcome.case_id}: no baseline outcome recorded")
            continue
        before = str(entry.get("outcome"))
        was_wrong = set(entry.get("mismatched") or ())
        new_slots = sorted(set(outcome.mismatched) - was_wrong)
        if was_wrong & WHOLE_QUERY_MISMATCHES:
            new_slots = []
        fixed_slots = sorted(was_wrong - set(outcome.mismatched))
        detail = f"(status={outcome.status}, mismatched={list(outcome.mismatched)})"
        if OUTCOME_RANK[outcome.outcome] < OUTCOME_RANK[before]:
            regressions.append(f"{outcome.case_id}: {before} -> {outcome.outcome} {detail}")
        elif new_slots:
            regressions.append(f"{outcome.case_id}: now also wrong in {new_slots} {detail}")
        elif OUTCOME_RANK[outcome.outcome] > OUTCOME_RANK[before]:
            improvements.append(f"{outcome.case_id}: {before} -> {outcome.outcome}")
        elif fixed_slots:
            improvements.append(f"{outcome.case_id}: no longer wrong in {fixed_slots}")
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
ANSWER_REL_TOL = 1e-8
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
    does. A dimension that a filter pins to one value is left out, as it is
    from the ``group_by`` slot.
    """

    from semantic_rails.ast import normalize_query

    normalized = normalize_query(copy.deepcopy(dict(query))).to_dict()
    select = {
        str(item["as"]): _value_column(item["expression"], aggregations)
        for item in normalized["select"]
    }
    pinned = _pinned_dimensions(normalized)
    named: dict[str, str] = {}
    for key in dict.fromkeys(str(key) for row in rows for key in row):
        if key.startswith("temporal_role."):
            if trend:
                named[key] = TIME_COLUMN
        elif key not in pinned:
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

    Row order matters only for ranking questions. Both sides are
    canonicalized, so a hand-edited ``939`` still equals ``939.0``.
    """

    alignment = _column_alignment(expected["columns"], actual["columns"])
    if alignment is None or len(expected["rows"]) != len(actual["rows"]):
        return False
    wanted = [[_canonical_value(value) for value in row] for row in expected["rows"]]
    rows = [[_canonical_value(row[index]) for index in alignment] for row in actual["rows"]]
    if ordered:
        return all(map(_same_row, wanted, rows))
    remaining = list(rows)
    for row in wanted:
        index = next(
            (i for i, candidate in enumerate(remaining) if _same_row(row, candidate)), None
        )
        if index is None:
            return False
        remaining.pop(index)
    return True


def query_answer(
    client: QueryMCPClient,
    query: Mapping[str, Any],
    case: Mapping[str, Any],
    aggregations: Mapping[str, str],
) -> dict[str, Any]:
    """Execute ``query`` and reduce its rows to an answer table for ``case``."""

    payload = client.tool_payload(
        "execute", {"query": {**dict(query), "limits": {"max_rows": GOLD_MAX_ROWS}}}
    )
    if not payload.get("ok"):
        codes = [issue.get("code") for issue in payload.get("errors") or []]
        raise ValueError(f"{case['id']}: query failed: {codes}")
    if payload.get("truncated"):
        raise ValueError(f"{case['id']}: answer was truncated")
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
                actual = query_answer(client, case["gold_query"], case, aggregations)
                frozen = case.get("gold_result")
                if not actual["rows"]:
                    problems.append(f"{case['id']}: gold answer is empty")
                if not isinstance(frozen, Mapping) or not answers_match(
                    frozen, actual, ordered=ordered
                ):
                    problems.append(f"{case['id']}: gold answer no longer matches the frozen one")
                for index, alternative in enumerate(case.get("alternatives") or []):
                    answer = query_answer(client, alternative, case, aggregations)
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
            answer = query_answer(client, case["gold_query"], case, aggregations)
            rows = [[_stored_value(value) for value in row] for row in answer["rows"]]
            if not case.get("ordered"):
                rows.sort(key=lambda row: [_sort_key(value) for value in row])
            case["gold_result"] = {"columns": answer["columns"], "rows": rows}
    path.write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")


# ---------------------------------------------------------------------------
# Report and CLI
# ---------------------------------------------------------------------------


def plan_baseline_json(outcomes: Sequence[PlanOutcome], cases: Sequence[Mapping[str, Any]]) -> str:
    """The plan baseline document, one case per line so a change is a one-line diff."""

    entries = [
        f"  {json.dumps(o.case_id)}: "
        + json.dumps({"outcome": o.outcome, "mismatched": list(o.mismatched)})
        for o in outcomes
    ]
    return (
        "{\n"
        f' "eval_set_sha256": {json.dumps(eval_set_digest(cases))},\n'
        f' "summary": {json.dumps(plan_summary(outcomes))},\n'
        ' "cases": {\n' + ",\n".join(entries) + "\n }\n}\n"
    )


def write_baseline(
    metrics: Mapping[str, int], outcomes: Sequence[PlanOutcome], cases: Sequence[Mapping[str, Any]]
) -> None:
    budgets = load_budgets() if BUDGETS_PATH.exists() else {}
    tolerance = float(budgets.get("tolerance", DEFAULT_TOLERANCE))
    document = {
        "description": (
            "Ceilings for scripts/mcp_context.py measurements, in tokens = round(chars / 4). "
            "Gated token metrics fail CI above budget + max(8, budget * tolerance), counts "
            "above budget; tracked ones are reported only. Regenerate with --write-baseline "
            "and review the diff."
        ),
        "tolerance": tolerance,
        "gated": rebaseline(
            {name: value for name, value in metrics.items() if name.startswith("query.")},
            budgets.get("gated", {}),
            tolerance,
        ),
        "tracked": rebaseline(
            {name: value for name, value in metrics.items() if name.startswith("architect.")},
            budgets.get("tracked", {}),
            tolerance,
        ),
    }
    BUDGETS_PATH.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    PLAN_BASELINE_PATH.write_text(plan_baseline_json(outcomes, cases), encoding="utf-8")


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
        [category, sum(counts.values()), *(counts[outcome] for outcome in OUTCOMES)]
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
            f"plan: {summary['cases']} cases: "
            + ", ".join(f"{summary[outcome]} {OUTCOME_LABELS[outcome]}" for outcome in OUTCOMES)
        )
        lines.extend(" ".join(str(cell) for cell in row) for row in case_rows)
        return "\n".join(lines)
    return "\n\n".join(
        [
            "### Context budgets (tokens = chars/4)",
            _markdown_table(["metric", "measured", "budget", "status"], budget_rows),
            "### Planner accuracy (plan detail=query)",
            _markdown_table(
                ["category", "cases", *(OUTCOME_LABELS[outcome] for outcome in OUTCOMES)],
                category_rows,
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
        help=(
            "Score a copy of a frozen split (such as the held-out one) instead; "
            "prints aggregates only."
        ),
    )
    parser.add_argument(
        "--allow-unfrozen",
        action="store_true",
        help="With --eval-file, score a file that matches neither frozen split.",
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
            digest = eval_set_digest(cases)
            frozen = {DEV_SET_SHA256: "dev", HELDOUT_SET_SHA256: "heldout"}.get(digest)
            report: dict[str, Any] = {"eval_set_sha256": digest, "frozen_split": frozen}
            if frozen is None and not args.allow_unfrozen:
                # A modified copy of a split isn't that split, whatever its labels say.
                print(json.dumps(report, indent=1))
                print(
                    "FAIL eval file matches neither frozen split; "
                    "pass --allow-unfrozen to score it anyway",
                    file=sys.stderr,
                )
                return 1
            problems = check_gold_answers(package, cases)
            report["plan"] = plan_summary(run_plan_accuracy(package, cases))
            report["gold_problems"] = len(problems)
            print(json.dumps(report, indent=1))
            return 1 if problems else 0
        cases = load_eval_cases()
        metrics = measure_query_mcp(package)
        with tempfile.TemporaryDirectory(prefix="mcp-context-architect-") as workspace:
            metrics.update(measure_architect_mcp(Path(workspace)))
        outcomes = run_plan_accuracy(package, cases)
        gold_problems = check_gold_answers(package, cases)

    eval_problems = []
    if eval_set_digest(cases) != DEV_SET_SHA256:
        eval_problems.append(
            f"{EVAL_SET_PATH.relative_to(REPO_ROOT)} changed; update DEV_SET_SHA256 "
            "only in a reviewed revision of the eval set"
        )
    eval_problems += [f"gold {item}" for item in gold_problems]

    if args.write_baseline:
        if eval_problems:
            for item in eval_problems:
                print(f"FAIL {item}", file=sys.stderr)
            print("baselines not written: fix the eval set first", file=sys.stderr)
            return 1
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
    failures += eval_problems
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
