- A measure's `accumulation:` that would load as a different one is refused at load with
  `INVALID_CONFIG`, naming the measure and the key, instead of answering with another number.
  A `snapshot:` other than `start_of_period` or `end_of_period` read as the closing balance,
  and a `snapshot:` without `kind: stock` beside it summed the balance across days; both are
  refused on a measure and under `defaults.measure`. A measure's own `accumulation:` replaces
  `defaults.measure.accumulation` whole, so under such a default a measure that left out the
  `kind:` summed a stock, and a stock that left out the default's `snapshot:` read the
  closing balance; such a measure now names its `kind:`, and a stock its `snapshot:` when
  the default sets one. See [Measures](docs/PACKAGE_AUTHORING.md#measures).
