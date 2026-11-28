# Development

[CONTRIBUTING.md](CONTRIBUTING.md) owns scope, layout and the commands to run before a PR.
This file holds the day-to-day habits behind them.

## How we test

**Table-driven by default.** When several tests differ only in their inputs and expected
values, write one test and list the cases with `@pytest.mark.parametrize`, giving each case
an `id`. Write a separate test only when the setup or the assertion is different in kind.

**Fixtures own their resources.** A fixture that opens a DuckDB connection, a temporary
database file or a subprocess closes it itself. Use `yield` with the cleanup after it, or a
context manager. The same goes for helper functions: one that opens a connection either
closes it before returning or hands the connection to a fixture. Keep a connection shared
across tests in a fixture, with the widest scope that is safe.

The test run fails when a test or fixture leaves a DuckDB connection open, and it lists the
tests. Garbage collection is paused while a test body runs, so a connection held only by a
reference cycle counts too. In a test, build a runtime with the `owned_runtime` fixture; it closes
what it builds.

**No sleeps and no wall-clock guards.** Never wait a fixed time for something to happen, and
never assert that code ran faster than some number of seconds. Both pass or fail with the
speed of the machine. Wait on the condition itself (an event, a poll with a deadline that
only decides when to give up), or inject the clock. The per-test timeout in `pyproject.toml`
is the only time limit.

**Smaller habits.**

- Assert on the behaviour a user sees, and keep each test independent of the others.
- Write tests that need a warehouse other than DuckDB under `tests/integration`.
- Use `tmp_path` for files, and `copy_package_config(..., writable=True)` for a package that
  a test edits.
