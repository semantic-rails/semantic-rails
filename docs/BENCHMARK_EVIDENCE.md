# Benchmark Evidence

Semantic Rails keeps benchmark evidence scoped to measured, reproducible behavior. Do not use these harnesses to make unmeasured competitor claims.

For agentic query governance, the benchmark question is whether an agent can resolve business
language into governed semantic IDs, validate the query shape, compile reviewable SQL, and execute
only after passing policy and path checks. The scorecards below measure that governed loop on the
checked-in corpus; they are not competitor benchmarks unless external systems are run and recorded
separately.

## Plan Gate

Run the deterministic plan gate before publishing benchmark or agent-readiness claims:

```bash
uv run python scripts/benchmark_plan.py --gate \
  --output benchmarks/scorecards/plan_gate.json \
  --markdown-output benchmarks/scorecards/plan_gate.md
```

The gate uses `scripts/blind_agent_corpus.json` plus built-in adversarial probes. It verifies:

- expected-answer intents remain actionable
- `detail="best"` is smaller than `detail="full"` overall and per expected-answer case
- expected semantic IDs and Query IR patches survive planning
- forbidden hallucinated IDs are not emitted
- `status="ok"` plans compile through the plan-warmed cache
- cache-hit compile p95 stays under the configured threshold

The optional JSON scorecard is intended for release evidence. Treat latency fields as local-run observations, not universal service-level claims.

## What The Score Means

The scorecard focuses on operational governance, not SQL-generation flair:

- coverage: expected questions produce actionable governed plans
- fidelity: plans resolve the semantic IDs expected by the corpus
- repairability: expected Query IR patches survive planning
- scope control: adversarial or off-topic prompts do not become executable SQL
- ergonomics: compact planning output is materially smaller than full debug output
- runtime behavior: compile after plan hits the warmed cache

## Generated Pattern Corpus

Run the generated corpus when changing planner pattern routing:

```bash
uv run python scripts/generate_benchmark_corpus.py
```

This writes `benchmarks/scorecards/generated_plan_corpus.json`. Its headline signal is expected-pattern-fired rate for intents generated from known planner pattern vocabulary. Rows that miss are planning-gap evidence, not competitor comparison evidence.

## Comparison Boundary

Comparative benchmark claims require each compared system to run from committed artifacts with the
same question set, documented setup, raw outputs, and a clear unsupported-case policy. Without that
evidence, cite these scorecards as Semantic Rails release evidence only.

## Related Regression Tests

These tests keep the benchmark contract connected to runtime behavior:

- `tests/semantic_rails/test_plan_scope_gate.py`
- `tests/semantic_rails/test_plan_intent_priority.py`
- `tests/semantic_rails/test_agentic_governance_program.py`
- `tests/semantic_rails/test_query_ir_schema.py`

Use this bundle with the benchmark scripts when reviewing agentic governance or scorecard changes:

```bash
uv run pytest -q \
  tests/semantic_rails/test_plan_scope_gate.py \
  tests/semantic_rails/test_plan_intent_priority.py \
  tests/semantic_rails/test_agentic_governance_program.py \
  tests/semantic_rails/test_query_ir_schema.py
```
