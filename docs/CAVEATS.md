# Caveat history

Use dated caveats in `caveats.yml` to explain changes in how a metric should be
read. Each entry can record who to ask (`owner`), when it was recorded
(`recorded_at`), and the pull request that documents it (`source_pr`).

```yaml
semantic_caveats:
  - id: caveat.sales.aov_before_2024
    kind: definition_change
    message: Average order value includes sales tax in this period.
    object_ids: [metric.sales.aov_usd]
    owner: sales_analytics
    recorded_at: "2023-01-03"
    source_pr: "#41"
    valid_to: "2024-01-01"
  - id: caveat.sales.aov_from_2024
    kind: definition_change
    message: Average order value excludes sales tax from January 2024.
    object_ids: [metric.sales.aov_usd]
    owner: sales_analytics
    recorded_at: "2024-01-02"
    source_pr: https://github.com/example/sales-model/pull/82
    supersedes: caveat.sales.aov_before_2024
    valid_from: "2024-01-01"
```

Dates use `YYYY-MM-DD`. Quote a `#N` reference in YAML. Validity uses
the query's clock and [half-open time windows](QUERY_CALL.md#time-windows-in-depth).
`valid_from` includes the first day and `valid_to` excludes the last bound.
Omitting a bound leaves that side open. Each metric's dated and linked
caveats must have disjoint windows. A replacement must name an existing
caveat with the same metric subjects and must keep the chain acyclic. Give the
predecessor an end and the replacement a start to mark the changeover.

An answer cites relevant caveats in `SEMANTIC_CAVEAT_APPLIED` warnings. A
window spanning a change can cite both caveats, each with its own validity
dates. For a call with no time bounds, the engine uses today's date in the
answer's clock. A dated caveat can also carry the existing `time` relevance
criteria, which still apply. Inspect cards list active, annotated caveats under
`card.caveats`. Both surfaces respect object visibility, audience and environment.

All five history fields are optional additions to package contract v1.
