"""MCP tool descriptions — must be agent guidance, not URL stubs.

Audit finding I4: every tool description used to be a one-liner ending
in 'Equivalent to POST /api/v1/X.' That's an HTTP-SDK stencil, not
agent-first guidance. An LLM ranking tools by description had no signal
about which tool to call first, no input-shape note, no gotcha. The
workflow itself is stated once, in the server instructions
(``MCP_SERVER_INSTRUCTIONS``; see test_mcp_instructions.py). This test pins
three constraints:

  1. Each description says when to use its tool (after/before/first/step).
  2. Each description names a concrete gotcha or call-shape note.
  3. Each description is non-trivial in length (>= 200 chars) so it
     can't silently regress to a one-liner.
"""

from __future__ import annotations

import copy

from semantic_rails.mcp import list_tool_definitions

LOOP_KEYWORDS = ("after", "before", "step", "first")
GOTCHA_KEYWORDS = ("gotcha", "must be", "do not", "don't", "skip", "only after", "cost")


def test_every_tool_description_says_when_to_use_it():
    for tool in list_tool_definitions():
        desc = tool["description"].lower()
        assert any(kw in desc for kw in LOOP_KEYWORDS), (tool["name"], tool["description"])


def test_every_tool_description_names_a_gotcha_or_call_shape():
    tools = list_tool_definitions()
    for tool in tools:
        desc = tool["description"].lower()
        assert any(kw in desc for kw in GOTCHA_KEYWORDS), (
            f"tool {tool['name']!r} description should name a gotcha or "
            f"call-shape note; got: {tool['description']!r}"
        )


def test_every_tool_description_is_non_trivial():
    tools = list_tool_definitions()
    for tool in tools:
        desc = tool["description"]
        assert len(desc) >= 200, (
            f"tool {tool['name']!r} description is too short ({len(desc)} chars); "
            f"agent-first descriptions need at least a what + when + gotcha. "
            f"Got: {desc!r}"
        )
        # Catch the old URL-stub regression: descriptions that are ONLY
        # the http equivalence sentence.
        without_http = desc.split("Equivalent to POST")[0].strip()
        assert len(without_http) >= 150, (
            f"tool {tool['name']!r} description is mostly the HTTP-equivalence "
            f"sentence; add what/when/gotcha. Got: {desc!r}"
        )


def test_every_tool_description_under_700_chars():
    """Upper bound so descriptions stay scannable in tools/list. execute carries the one
    Query IR cheat-sheet, including every query-time variant, so it gets more room."""
    tools = list_tool_definitions()
    for tool in tools:
        desc = tool["description"]
        assert len(desc) <= (1200 if tool["name"] == "execute" else 700), (
            f"tool {tool['name']!r} description is too long ({len(desc)} chars); "
            f"keep agent-rated descriptions scannable."
        )


def test_plan_description_mentions_out_of_scope_branch():
    """Fix 1 added 'out_of_scope' / 'low_relevance' blocks. The plan
    description must teach the agent to branch on them so the LLM doesn't
    blindly trust ``best`` when both fields are present."""
    tools = list_tool_definitions()
    plan = next(t for t in tools if t["name"] == "plan")
    desc = plan["description"].lower()
    assert "out_of_scope" in desc or "low_relevance" in desc, (
        f"plan description should mention the empty-result blocks; got: {plan['description']!r}"
    )


def test_ir_accepting_tool_descriptions_enumerate_select_expression_shapes():
    """The handoff finding F4: blind agents had no signal about which
    `select.expression` shapes are accepted. The cheat-sheet ships once, on
    `execute`, the only tool that takes Query IR to run."""
    tools = list_tool_definitions()
    execute_desc = next(t["description"] for t in tools if t["name"] == "execute")
    for shape in ("{aggregation, measure}", "{metric}", "prior_period"):
        assert shape in execute_desc, (
            f"execute description must enumerate {shape}; got {execute_desc!r}"
        )


def test_execute_description_names_every_query_time_variant(runtime_factory):
    """Define a measure once, vary it per question: the shipped text must name each variant
    the Query IR composes at query time, and its example must validate."""
    import json
    import re

    from semantic_rails.mcp import MCP_SERVER_INSTRUCTIONS, SemanticLayerMCPAdapter

    execute_desc = next(t["description"] for t in list_tool_definitions() if t["name"] == "execute")
    for variant in (
        "{aggregation, measure}",
        "filter:{all:[{field,op,value}]}",
        "kind:rolling",
        "kind:prior_period",
        "kind:period_to_date",
        "kind:cumulative",
        "kind:conversion",
        "scoped_aggregate",
        "aggregate_if",
        "metric_filters[]",
        "kind:metric_predicate",
    ):
        assert variant in execute_desc, variant
    assert "composes at query time with no model change" in MCP_SERVER_INSTRUCTIONS
    example = re.search(r"large orders: (\{.*\})\.$", execute_desc)
    assert example, execute_desc
    expression = json.loads(re.sub(r"(\w+):", r'"\1":', example.group(1)).replace("'", '"'))
    adapter = SemanticLayerMCPAdapter(runtime_factory("jaffle_shop"))
    try:
        query = {"version": 2, "select": [{"as": "large", "expression": expression}]}
        response = adapter.call_tool("execute", {"query": query, "mode": "validate"})
    finally:
        adapter.close()
    assert response["ok"], response["errors"]


def test_no_orphan_explain_tool_references():
    """The `explain` tool was removed; the field on compile's response
    still exists. Tool descriptions and prompts must not reference
    `'explain'` (quoted) as a callable tool — only as a payload field
    (e.g. `compile's response includes an explain payload`)."""
    from semantic_rails.mcp import (
        MCP_PROMPT_DEFINITIONS,
        MCP_TOOL_DEFINITIONS,
    )

    for tool in MCP_TOOL_DEFINITIONS:
        desc = str(tool["description"])
        if tool["name"] == "segment":  # 'explain' is one of its actions
            continue
        assert "'explain'" not in desc, (
            f"tool {tool['name']!r} description references removed tool 'explain': {desc!r}"
        )
    for prompt in MCP_PROMPT_DEFINITIONS:
        desc = str(prompt["description"])
        # Bare ", and explain" in a comma list refers to the tool, not
        # the payload field.
        assert ", and explain" not in desc, (
            f"prompt {prompt['name']!r} lists removed tool 'explain': {desc!r}"
        )


def test_ir_accepting_tool_descriptions_teach_positional_shapes():
    """Round-six finding B6: select/group_by/where/order_by use three
    different key vocabularies (`as`, bare string, `field`). A blind
    agent had to discover each by validator rejection. The positional
    teach ships once, on execute.
    """
    tools = list_tool_definitions()
    execute_desc = next(t["description"] for t in tools if t["name"] == "execute")
    for marker in ("select[]", "group_by[]", "where[]", "order_by[]"):
        assert marker in execute_desc, (
            f"execute description must teach the {marker} shape; got {execute_desc!r}"
        )


def test_range_last_query_schema_describes_object_shape():
    """Round-six finding B1: the QUERY_SCHEMA `range.last` used to say
    `string` and showed `{last: '90 days'}` while the runtime + JSON
    Schema both demand `{unit, value}`. Pin the corrected schema."""
    from semantic_rails.mcp import QUERY_SCHEMA

    time_block = QUERY_SCHEMA["properties"]["time"]
    range_block = time_block["properties"]["range"]
    last = range_block["properties"]["last"]
    assert last["type"] == "object", f"range.last must be declared as object; got {last['type']!r}"
    assert set(last["required"]) == {"unit", "value"}, (
        f"range.last must require unit + value; got required={last['required']!r}"
    )
    assert "unit" in last["properties"]
    assert "value" in last["properties"]
    # Description should also drop the misleading string shorthand.
    desc = range_block["description"]
    assert "unit" in desc and "value" in desc, (
        f"range block description should mention unit + value; got {desc!r}"
    )


def test_expression_shapes_examples_parse(runtime_factory):
    """Round-six finding B2: `_EXPRESSION_SHAPES["rolling"].example` was
    `{window: 7, grain: "day"}` — both invalid (window must be dict; grain
    is not a valid key). Verify every shipped example actually parses
    through the AST so future shape edits stay self-consistent."""
    from semantic_rails.expressions import parse_semantic_expression
    from semantic_rails.metadata_parts.capabilities import _EXPRESSION_SHAPES

    runtime = runtime_factory("jaffle_shop")
    try:
        for shape in _EXPRESSION_SHAPES:
            # Deep copy: the placeholder swap must not edit the shared shapes.
            example = copy.deepcopy(shape["example"])
            # Replace placeholder ids with real ones the parser can resolve
            # at validate-time. The parser only checks the IR shape; entity
            # resolution happens later.
            _replace_placeholder_ids(example)
            # parse_semantic_expression raises SemanticLayerError on shape
            # problems — the test passes as long as no exception escapes.
            parse_semantic_expression(example, context="query")
    finally:
        runtime.close()


def _replace_placeholder_ids(node):
    """Walk an example dict and replace `measure.<id>`-style placeholders
    with concrete jaffle_shop ids that exist. The parser does not check
    these against the package, but downstream identifiers like
    `entity.<id>` raise on validation. Keep it light."""
    placeholders = {
        "measure.<id>": "measure.jaffle.revenue_usd",
        "measure.<num>": "measure.jaffle.revenue_usd",
        "measure.<den>": "measure.jaffle.order_count",
        "measure.<start>": "measure.jaffle.session_count",
        "measure.<end>": "measure.jaffle.order_count",
        "metric.<id>": "metric.sales.aov_usd",
        "entity.<id>": "entity.jaffle_order",
    }
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if isinstance(value, str) and value in placeholders:
                node[key] = placeholders[value]
            else:
                _replace_placeholder_ids(value)
    elif isinstance(node, list):
        for item in node:
            _replace_placeholder_ids(item)


def test_expression_shape_examples_validate_as_query_ir(runtime_factory):
    """Round-eight: the slim MCP tool descriptions point agents at
    ``capabilities.expression_shapes[].example`` as the rescue path
    for advanced kinds (prior_period, rolling, ...). Parsing the
    example is necessary but not sufficient — the agent will wrap it
    in ``{select: [{expression: <example>, as: ...}]}`` and call
    ``validate``. If validate rejects the wrapped form with a
    shape-related error, the rescue path is broken.

    Wrap each shape's example, replace placeholder ids with concrete
    jaffle_shop ids, and assert validate does not return shape errors
    (errors whose path points INTO ``select`` or whose code names a
    structural problem with the expression).
    """
    from semantic_rails.mcp import SemanticLayerMCPAdapter
    from semantic_rails.metadata_parts.capabilities import _EXPRESSION_SHAPES

    # Some shapes need scaffolding beyond a single select expression:
    # - conversion + aggregate_if reference column refs that must bind
    #   to a real entity column; jaffle_shop's order entity has a
    #   `status` column for aggregate_if and a `customer_id` for joins.
    # - distribution requires `over.entity` to be a real entity id.
    # The placeholder map below mirrors _replace_placeholder_ids but
    # adds `<col>` -> 'status' for the aggregate_if shape.
    placeholders = {
        "measure.<id>": "measure.jaffle.revenue_usd",
        "measure.<num>": "measure.jaffle.revenue_usd",
        "measure.<den>": "measure.jaffle.order_count",
        "measure.<start>": "measure.jaffle.session_count",
        "measure.<end>": "measure.jaffle.order_count",
        "metric.<id>": "metric.sales.aov_usd",
        "entity.<id>": "entity.jaffle_order",
        "<col>": "status",
    }

    def replace(node):
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if isinstance(value, str) and value in placeholders:
                    node[key] = placeholders[value]
                else:
                    replace(value)
        elif isinstance(node, list):
            for item in node:
                replace(item)

    # Some kinds require an outer time block (e.g. prior_period offsets
    # imply a temporal anchor; cumulative inherits grain from outer
    # time.grain). Provide a minimal jaffle-compatible time block when
    # the kind asks for one — the parse-only test doesn't cover this.
    needs_time = {"prior_period", "cumulative", "rolling"}

    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        failures = []
        for shape in _EXPRESSION_SHAPES:
            # Deep copy: the placeholder swap must not edit the shared shapes.
            example = copy.deepcopy(shape["example"])
            replace(example)
            query_ir: dict = {
                "select": [{"expression": example, "as": "_test"}],
            }
            if shape["name"] in needs_time:
                query_ir["time"] = {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "month",
                }
            response = adapter.call_tool("execute", {"mode": "validate", "query": query_ir})
            errors = response.get("errors") or []
            # Shape failures = any error pointing into select.expression
            # OR a structural code like INVALID_EXPRESSION_SHAPE.
            # Shape-related error codes that would indicate the example
            # itself is malformed (vs. a missing package object or
            # boundary issue we don't care about for this test).
            shape_codes = {
                "INVALID_EXPRESSION_AST",
                "UNKNOWN_EXPRESSION_KIND",
                "INVALID_EXPRESSION",
                "INVALID_EXPRESSION_SHAPE",
                "USE_OBJECT_SHAPE",
            }
            shape_errors = [
                e
                for e in errors
                if (
                    e.get("code") in shape_codes
                    or "expression" in str(e.get("path", "")).lower()
                    or "expression" in str(e.get("details", {}).get("path", "")).lower()
                    or "shape" in str(e.get("code", "")).lower()
                )
            ]
            if shape_errors:
                failures.append((shape["name"], shape_errors))
        assert not failures, (
            "capabilities.expression_shapes[].example must produce no "
            "shape-related validate errors when wrapped as Query IR. "
            f"Failures: {failures}"
        )
    finally:
        adapter.close()


def test_discover_minimal_verbosity_slims_records(runtime_factory):
    """The 'minimal' verbosity on discover (the MCP default) keeps a slim
    card: what an agent needs to pick a candidate and tell near-duplicates
    apart (id, kind, label, score, a short description,
    default_temporal_role, available, and blocked_reason when unavailable).
    Ranking and debug detail (name, topics, match_reasons,
    recommended_next_actions, comparison metadata, starter_query_patch)
    must NOT appear.

    Also verify the wire size shrinks materially — minimal should be
    a fraction of compact, otherwise the verbosity level is doing no
    work."""
    import json

    from semantic_rails.mcp import SemanticLayerMCPAdapter

    runtime = runtime_factory("jaffle_shop")
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        compact = adapter.call_tool(
            "discover", {"terms": "orders by store", "verbosity": "compact"}
        )
        minimal = adapter.call_tool(
            "discover", {"terms": "orders by store", "verbosity": "minimal"}
        )

        # Both must be ok and ship records.
        assert compact.get("ok") is not False
        assert minimal.get("ok") is not False
        # Discover returns a flat envelope (no separate `payload` key).
        compact_payload = compact
        minimal_payload = minimal

        permitted = {
            "id",
            "kind",
            "label",
            "score",
            "description",
            "default_temporal_role",
            "available",
        }
        for bucket in ("measures", "metrics", "dimensions"):
            rows = minimal_payload.get(bucket) or []
            assert rows, f"minimal discover should still return {bucket} rows"
            for row in rows:
                allowed = permitted | (
                    {"blocked_reason"} if row.get("available") is False else set()
                )
                extra = set(row.keys()) - allowed
                assert not extra, f"minimal verbosity {bucket} row leaked verbose keys: {extra}"
                assert len(row.get("description", "")) <= 120

        # Wire-size check: minimal must be materially smaller than compact.
        compact_bytes = len(json.dumps(compact_payload))
        minimal_bytes = len(json.dumps(minimal_payload))
        assert minimal_bytes < compact_bytes * 0.5, (
            f"minimal ({minimal_bytes:,}B) should be <50% of compact "
            f"({compact_bytes:,}B); otherwise the verbosity is doing no work."
        )
    finally:
        adapter.close()


def test_capabilities_payload_ships_expression_shapes(runtime_factory):
    """Companion structured surface for F4: capabilities exposes the
    expression shapes as a queryable list of {name, description, example}
    dicts. Lets agents introspect at runtime without parsing description text."""
    from semantic_rails.metadata_parts.capabilities import capabilities_payload

    runtime = runtime_factory("jaffle_shop")
    try:
        payload = capabilities_payload(runtime)
        shapes = list(payload.get("expression_shapes") or [])
        assert shapes, "capabilities.expression_shapes must be non-empty"
        names = {str(s["name"]) for s in shapes}
        assert {
            "aggregate",
            "metric",
            "prior_period",
            "rolling",
            "cumulative",
            "ratio",
            "conversion",
            "distribution",
        } <= names, f"capabilities.expression_shapes missing kinds; got names={names}"
        for shape in shapes:
            assert isinstance(shape, dict)
            assert shape.get("name"), f"shape missing name: {shape}"
            assert shape.get("description"), f"shape missing description: {shape}"
            assert isinstance(shape.get("example"), dict), f"shape missing example dict: {shape}"
    finally:
        runtime.close()
