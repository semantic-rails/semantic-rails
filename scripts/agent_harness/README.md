# Agent harness

`run.py` lets a model behind any OpenAI-compatible chat API with tool calls (a local
llama.cpp, vLLM or similar server, or a hosted one) drive the Semantic Rails MCP servers the
way an MCP host would, or the CLI and REPL through a terminal the way a person would. It records every turn and tool call, the tokens the server reports, and
where the model struggled. `report.py` ranks that friction across runs.

It answers two questions:

- **What does a task cost?** Turns, tool calls, prompt, completion and reasoning tokens,
  the peak context, and wall time. Compare two tool surfaces by running the same scenarios
  against each.
- **Where does a less capable model get stuck?** A smaller local model trips over unclear
  tool descriptions, error messages and defaults that a frontier model works around. The
  friction report turns those into a ranked list.

`scripts/mcp_context.py` measures the query MCP's payload sizes offline; this harness runs
an actual model.

## Run a scenario

```bash
make install
uv run python scripts/agent_harness/run.py scripts/agent_harness/scenarios/draft_model.yml \
  --out ../agent-runs/draft_model-1 --model qwen3.8-27b \
  --base-url http://127.0.0.1:8081/v1 --reasoning-effort low
```

`uv run` puts the engine's `semantic-rails` and `semantic-rails-architect-mcp` on `PATH` for
the servers, setup and check. To use a model server on another machine, forward its port
first, for example `ssh -N -L 8081:127.0.0.1:8081 <model-host>`. Set `AGENT_API_KEY` if the
endpoint needs a bearer token.

The loop sends one request at a time and waits for each reply. A lock in the system temp
folder refuses a second run on the same machine, so runs never compete for one model server.

Options: `--max-turns` and `--max-tokens` override the scenario, `--turn-tokens` caps each
reply (default 8192), `--timeout` bounds the agent loop (default 1800 seconds),
`--result-chars` truncates results in the transcript (default 2000; `0` keeps them whole;
the model always gets the whole result). `--server NAME=COMMAND` adds a server or replaces
the scenario's server of that name. `--workdir DIR` works in an existing folder instead of a
fresh one. `--jail PREFIX` starts every server and terminal program under a command prefix,
such as a namespace wrapper that takes away the network.

## Scenario files

```yaml
task: |                      # the user's request, in plain language
  My shop's data is in the DuckDB file shop.duckdb. ...
servers:                     # MCP stdio servers, started in the workdir
  architect: semantic-rails-architect-mcp --workspace-root .
setup: ...                   # optional shell command that prepares the workdir
check: ...                   # shell command; exit 0 means the task succeeded
max_turns: 20                # default 30
max_tokens: 300000           # stop once prompt + completion tokens reach this; default 500000
terminal:                    # optional programs the model may run in a terminal
  repl: semantic-rails repl
```

In `servers`, `setup` and `check`, `{repo}` is this repository, `{here}` the scenario's
folder and `{python}` the harness's Python. The check runs in the workdir after the loop,
with `AGENT_FINAL_ANSWER` naming a file that holds the model's last reply. Servers get the
MCP SDK's default environment (`HOME`, `PATH` and a few more); pass anything else as
`env NAME=value command`. `summary.json` records every server command as written, so never put
a secret in one.

| Scenario | Server | The user asks |
|---|---|---|
| `draft_model.yml` | Architect | which tables a DuckDB file holds, then a project with a model for one of them |
| `ratio_metric.yml` | Architect | an average-order-value ratio metric in an existing project |
| `filtered_question.yml` | query | one store's revenue in one quarter of the bundled `jaffle_shop` sample |
| `repl_revenue.yml` | terminal | each store's monthly revenue for one quarter, through the REPL |

## The terminal

A scenario's `terminal` names the programs the model may start. It then gets four tools:
`term_start` (a program by name, with extra arguments), `term_type` (text, then Enter unless
`enter` is false), `term_key` (Enter, Tab, arrows, Escape, Backspace, Ctrl-C, Ctrl-D) and
`term_read` (wait for more output). The program runs in a pseudo-terminal, started directly
with no shell, with `TERM=dumb` and `NO_COLOR` so it prints plain text. Each tool returns what
the program printed since the last call, once output has paused for 1.5 seconds (or after
`wait_ms`, default 10 seconds, with none), escape sequences removed and at most the last 6,000
characters. One program runs at a time; starting another stops it. Its errors, repeats and
wasted tokens count like any tool's.

## The run folder

`--out` names a new folder; an existing one is never reused.

- `transcript.jsonl`: a `tools` event (the tool names and the size of their schemas), then one
  `turn` event per model reply (tokens, finish reason, seconds, text, the tools it called) and
  one `call` event per tool call (arguments, error, argument problems, whether it repeats an
  earlier call, the result and its full length, seconds).
- `summary.json`: the scenario, model and servers; `stop` (`final`, `length`, `max_turns`,
  `max_tokens`, `loop`, `timeout`, `no usage reported` or `request failed: …`); `finished`;
  `success` (the check passed); turns, tool calls and errors; token totals and
  `peak_prompt_tokens`; the tools never called; and `friction` per tool.
- `final_answer.txt`, `servers.log` (the servers' stderr), and `workdir/`.

Every turn resends the whole conversation, so `prompt_tokens` sums what a client without
prompt caching pays. A token category the server doesn't report is `null` and listed in
`unavailable`, never counted as 0; a run whose server reports no prompt or completion tokens
stops after its first tool turn (`no usage reported`), since its token budget can't be
enforced. A reply cut off by `--turn-tokens` (`length`) ends the run without running its tool
calls. The model sees the compact `structuredContent` of each result, as
`mcp_context.py` measures it, or the text blocks when there is none.

**Friction, per tool:**

- `errors` and `errors_seen`: calls that failed, and their messages (`CODE: message` when the
  tool returns a structured error). Unknown tool names and arguments that are not a JSON
  object count as errors of the name the model used.
- `bad_arguments`: calls missing a required argument or passing one the schema doesn't
  declare, or whose arguments were not a JSON object.
- `repeats`: calls identical to an earlier call in the run. Three identical calls in a row
  stop the run (`loop`).
- `wasted_tokens`: for each failed or repeated call, its share of the tokens of the turn that
  made it.

## Rank the friction

```bash
uv run python scripts/agent_harness/report.py ../agent-runs/*/
```

It prints one row per run, then the tools ranked by wasted tokens, then errors, each with its
most frequent error, and the tools no run called.

## Compare two tool surfaces

Run the same scenarios once per surface, changing only the server, then compare the
summaries and the two reports:

```bash
for surface in a b; do
  uv run python scripts/agent_harness/run.py scripts/agent_harness/scenarios/filtered_question.yml \
    --out "../agent-runs/question-$surface" --model qwen3.8-27b \
    --server "query=env SURFACE_SETTING=$surface semantic-rails mcp stdio --path jaffle_shop"
done
```

Replace `SURFACE_SETTING` with the setting that selects the surface. A model's runs vary, so
repeat each scenario a few times before drawing a conclusion.
