- The semantic-layer comparison pack generates every support label from one executable rubric
  (`comparisons/semantic_layers/shared/rubric.md`). The rubric applies the same rules to every
  layer, Semantic Rails included, and publishes each label's evidence. The runners this pack
  re-runs now record only whether a question executed. Every layer answers q11 and q12 by
  reading a precomputed customer rollup column, so all six are labeled `precomputed` there.
  Semantic Rails was previously labeled `native`.
