- Caveats take an optional `so_what`: what the reader should do about the fact the
  `message` states. The `SEMANTIC_CAVEAT_APPLIED` warning carries `so_what` and `owner` at
  the default `compact` verbosity (and at `full`) when the caveat sets them. A non-string
  `so_what` is refused with `INVALID_CONFIG`. See
  [`caveats.yml`](docs/PACKAGE_AUTHORING.md#caveatsyml).
- A package test kind, `caveat_applies`, pins whether a call states a caveat: `caveat:`
  names it, and `applies: false` asserts that it is not stated. A failure reports
  `CAVEAT_APPLIES_MISMATCH` with the caveats the call did state; a refused call fails the
  test in either form. See [Examples and tests](docs/PACKAGE_AUTHORING.md#examples-and-tests).
