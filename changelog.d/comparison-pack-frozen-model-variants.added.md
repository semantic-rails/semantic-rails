- The comparison pack adds eight frozen-model questions (q17-q24). Each changes one parameter
  of a metric the first 16 questions use (a conversion window, a rolling window, a period
  offset, a per-metric filter, an aggregation or a threshold), and every layer answers it with
  its model unchanged, through its query-time interface only. The rubric gains a
  `requires_model_change` label, which `comparisons/semantic_layers/shared/frozen_model.yml`
  backs with a reason and a documentation link, and the published matrix gives each layer's
  count answered with the model frozen. Cube's runner now also sends SQL API queries through
  `/cubesql`.
