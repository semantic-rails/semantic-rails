- `plan` offers no draft on a measure the question doesn't name: "How many employees work at
  each store?" no longer drafts the open store count. It returns `PLAN_SUBJECT_UNMATCHED`
  naming the words no measure matches and the measures and metrics that mention them
  (`unrealizable` when none do). A measure that can't reach a dimension it groups or filters by
  returns `unrealizable` with `PLAN_UNCONNECTED` naming the pair, instead of the failing
  draft. A
  `PLAN_FALLBACK_SEMANTIC_DRIFT` message now says that `best.query` is the draft that fails,
  with its errors, and a fallback shown on a measure the question doesn't name says it is a
  placeholder not to run. A plan that is `ok` is unchanged.
- `plan` holds a "which" list whose only word naming a measure is the listed noun ("Which
  stores are there?"), as it holds such a ranking.
