# MCP Interface

`semantic_rails.mcp` exposes a dependency-free Model Context Protocol interface over the same
runtime that serves `/api/v1/*`. The canonical implementation remains the in-process
`SemanticLayerMCPAdapter`. The ASGI app serves stateless MCP Streamable HTTP at `/mcp`, while the
CLI supports stdio and stateless Streamable HTTP through `semantic-rails serve` at `/mcp`.

For `semantic-rails mcp stdio`, stdout contains only newline-delimited JSON-RPC messages;
startup diagnostics go to stderr. If the selected package cannot load, `initialize`
returns a JSON-RPC error with the engine's message, stable code in `error.data.code`,
and selected package directory or YAML file in `error.data.details.config_path`.
Existing engine details (such as the relationship and suggested fix) are preserved.
The server answers requests with that refusal until the client disconnects, then
exits with status 1. Fix the package and restart the server before querying it.

## Streamable HTTP Endpoint

A self-hosted ASGI process exposes:

```text
http://127.0.0.1:8080/mcp
```

It implements stateless MCP Streamable HTTP with protocol version `2025-11-25`.
`POST /mcp` accepts one JSON-RPC object and returns JSON; notifications return `202` with no body.
`GET /mcp` returns `405` because this transport does not require server-initiated SSE.
When `SEMANTIC_RAILS_API_KEYS` (or `SEMANTIC_RAILS_API_KEY_FILE`) is configured, `/mcp` requires
the same bearer API key as `/api/v1/*` and returns `401` otherwise.
Clients must send:

```text
Content-Type: application/json
Accept: application/json, text/event-stream
MCP-Protocol-Version: 2025-11-25
```

## Runtime Adapter

```python
from semantic_rails.mcp import SemanticLayerMCPAdapter

adapter = SemanticLayerMCPAdapter.from_package("jaffle_shop")
try:
    tools = adapter.list_tools()  # Paid once at connect time.
    found = adapter.call_tool("discover", {"terms": "orders by store name"})  # "" lists every id.
    draft = adapter.call_tool("plan", {"intent": "orders by store name"})
    if draft["status"] == "ok" and not draft["warnings"]:
        result = adapter.call_tool(
            "execute",
            {"query": draft["best"]["query_ir"], "row_format": "columns"},
        )
finally:
    adapter.close()
```

The adapter returns plain dictionaries with the same additive envelope fields used by the HTTP
API:

- `ok`
- `status`
- `api_version`
- `request_id`
- `package_id`
- `warnings`
- `errors`
- `error` (`{code, message}` from `errors[0]`, present for errors including validate soft-fails)
- `recovery_hints` (independent discovery next steps only; error hints live in `errors[i].recovery_hints`)
- `timing_ms`

Each full error issue appears once in `errors`, with its own `recovery_hints`;
the hints are not repeated at the response root, in `query_ir_hints`, or in that
issue's `details`. Distinct query-IR hints and independent discovery next steps remain.
MCP issues leave out empty optional fields and a
`why_invalid` or `unsupported_construct` that only repeats its `message` or `code`, and
`request_context` appears only when a transport or `policy_context` set one.

Within a stdio query MCP session, repeated successful calls still run normally and add
`same_as`, the first matching response's `request_id`. Matching uses the tool name
and arguments with JSON object keys sorted, ignoring `verbosity` and
`request_id` at the argument and query envelopes. Array order, filters, query
limits, `max_rows` and policy context still distinguish requests. If the first
matching run was capped, `same_as` is instead an object with `request_id`,
`row_count`, `truncated: true` and `max_rows` describing that historical response.
The current response retains its own truncation status. The session retains the
64 most recently used request fingerprints; evicted calls are forgotten.

After a successful `execute` in run mode, `execute` in `validate` or `sql` mode
for the same query and policy context also adds
`already_ran: {"request_id": "...", "row_count": 12}`. This refers to the latest
retained successful run and its returned row count. A capped run also carries
`truncated: true` and the effective `max_rows` cap in `already_ran`; a later
successful run replaces this history even when its cap or row format differs. It is a
historical hint, not a cached answer or a guarantee that warehouse data is unchanged.
These dry runs also add a short `next` string telling the agent to answer from
that result or change the query. On the second consecutive identical `validate`
of an already-run query, `next` directly tells the agent to stop validating it;
further consecutive validates keep that guidance. An intervening tool call,
changed arguments (other than response options), or a call without `already_ran`
resets the streak. SQL dry runs get the ordinary guidance and reset the streak.
This advice contains no result rows and never changes validation or refuses a call.
Unlike `plan`'s structured `next` object, `execute`'s `next` is a string.
The first call has no added fields. Stateless HTTP and calls without a session
retain their existing responses; REST, SDK and CLI query responses are unchanged.

In-process MCP hosts can create `MCPQuerySession` from
`semantic_rails.mcp_session` and pass it as `session=` to `adapter.call_tool` or
`handle_jsonrpc_message`. Use a separate instance for each client session.

Every `tools/list` definition publishes an `outputSchema` for this envelope and
MCP-standard annotations (`readOnlyHint`, `destructiveHint`,
`idempotentHint`, and `openWorldHint`). The complete generated contract is packaged as
`semantic_rails/contracts/query_mcp.v2.json`; CI compares it with the executable definitions so
tool/schema drift cannot be merged silently.

## Tools

- `discover`: rank objects against business terms; empty `terms` list the catalog's ids.
- `inspect`: one object's card.
- `valid-values`: a dimension's governed values.
- `plan`: draft Query IR from a natural-language question. A fully matched
  package example uses its validated authored query (`best.pattern:
  package_example`), including its groupings, filters and limit. An example
  answers only its exact question: case and whitespace aside (and the
  contractions `plan` expands in every question), any other wording, including
  punctuation, plurals, numbers, signs, symbols, a different top-N count or
  another date or time phrase, gets normal planning. Multiple
  valid visible matches return
  `needs_clarification` with `PLAN_AMBIGUOUS_EXAMPLE` and
  `why.details.example_ids`. Invalid examples fall through with
  `why.details.invalid_examples`; an example that reads a hidden object is
  invalid for that caller, as in a package without the object.
  Runtimes without a source path have no package examples.
- `execute` (`/api/v1/query`): validate, compile and run Query IR. `mode="validate"` or
  `mode="sql"` stops before running it.
- `segment`: `action="validate"`, `"explain"` or `"preview"` for a package-authored segment.
  Listed only when the current package defines segments; its instruction and workflow prompt
  are omitted otherwise. Tool availability follows `Runtime.reload()` on the same adapter.

`initialize` returns the workflow as server `instructions` (under 2KB): find objects with
`discover`, draft Query IR with `plan`, and run it with `execute`, which validates and compiles
first, so its `validate` and `sql` modes are optional dry runs. The instructions also carry the
conventions every tool shares: full ids, response detail controls, and recovery hints.
Instructions use static `jaffle_shop` examples for every package and never insert package
object ids. Without `time` or `where`, the query adds no time window or filter; metric
definitions and package policies still apply.
Each tool description then says what the tool does, when to use it, and its
one gotcha. Every tool returns its smallest response by default (`verbosity="minimal"`, `plan`
`detail="query"`); ask for more only when you need it.

Every tool accepts optional `request_id` and `policy_context` at runtime, including tools
with closed schemas, but leaves these transport fields out of its advertised input schema.
`policy_context` (`environment`, `audience`, `roles`) is for local testing; authenticated
transports supply the trusted context and ignore the argument.

`tools/list` is paid once at connect time, before the first call. To keep it bounded, the IR
cheat-sheet and the full Query-IR time-block schema ship once, on `execute`; the other
IR-accepting tools point at it. The description includes the expression-shape list, per-order ratio, arithmetic and
conditional-count hints, and lists rows with an empty `select`
and `group_by` alone. `validate` checks a query before it runs; a query that already ran
needs no validation.

### Writing Tool Descriptions

The query MCP follows these rules, and other Semantic Rails MCP servers can reuse them:

- **Workflow once.** State the order of calls and the conventions every tool shares in the
  server `instructions`, not in each description. Keep instructions under 2KB; hosts load them
  up front.
- **One description, three parts.** Say what the tool returns, when to use it (relative to
  other tools: "after `discover`", "before writing a `where` filter"), and its one gotcha,
  introduced with "Gotcha:". Aim for 200–700 characters; `execute` may use up to 850
  for its IR guidance while staying within the fixed context budgets.
- **No contradictions.** A description never tells the agent to call a tool that another
  description calls optional. If a step is optional, say so everywhere.
- **Real examples.** Tool-description example ids must exist in the bundled `jaffle_shop` package
  (`dimension.jaffle_store_name`, not `dimension.jaffle.store_name`).
- **Accurate cost claims.** Say which tools query the warehouse, and match the annotations
  (`readOnlyHint`, `openWorldHint`).
- **Parameters describe themselves.** When a parameter's name doesn't explain it, put its
  meaning in its schema (`enum`, `default`, a short `description`) rather than in prose. Keep
  transport fields `request_id` and `policy_context` accepted without advertising them.
- **Budgets.** `tests/semantic_rails/mcp_context/budgets.json` gates the size of `tools/list`
  and the instructions (see "Measuring Context Cost").

### Catalog And Metadata

`discover` with empty `terms` lists the catalog instead of ranking: counts, the package's
capability flags, and ids per object kind, 100 per kind at a time. `limit` and `offset` page the
ids, `kinds` limits the kinds listed, and a `DISCOVER_IDS_TRUNCATED` warning gives
`details.next_offset` while more remain. The `semantic-rails://catalog/index`, `catalog/summary`
and `catalog/full` resources return the whole index, descriptive rows, or every card with the
alias index (see [Resources And Prompts](#resources-and-prompts)).

`discover` returns slim cards by default: `id`, `label`, a `description` retaining whole sentences
up to 120 characters (sentences naming package dimensions, entities, measures or metrics survive
past the cap; descriptions repeating the label are omitted) and `default_temporal_role`, plus
`available: false` and `blocked_reason` for a candidate that isn't available. A card in a kind's
bucket leaves out its `kind`; the response leaves out the `terms` and `verbosity` it was called
with. `verbosity="compact"` keeps the same slim cards and descriptions but omits unavailable
candidates and the blocked bucket. Match reasons, starter patches, companions, recommended actions
and blocked metadata are retained with `verbosity="full"`, subject to the response budget. When the
question uses an object's whole name ("revenue by store"), that object ranks
above near-duplicates that add a qualifier the question doesn't use ("Delivered revenue").
`kinds` takes an array or a comma-separated string, and also a JSON array sent as a string.
Dimension-value cards keep the raw filter `value`, its business-facing `label`, and explicit
`available` flag, including when a value is blocked. Minimal cards omit `score`; order gives rank.
An aggregate metric at its measure's default aggregation, without filters, windows, parameters
or temporal pins, replaces its measure card when both are available in the response and neither
is named in a policy's `object_ids`. It carries `measure` with that measure's id. At most one
metric replaces each measure; additional equivalent metrics keep their own cards. Other metrics
and measure-only requests retain separate cards. Grant-scoped discovery retains its card fields,
including `starter_query_patch`, at every verbosity.

`inspect` (default `verbosity="minimal"`) states each fact once. It leaves out fields that
repeat another one (`object_type`, `usage_summary`, `top_values`), a description that only repeats
the label, generic `recommended_next_actions`, empty structural fields, and every starter patch after the first. Declared sample values
and query literals remain exact, including blank and null values. `"compact"` or `"full"` return
the whole card, which is also the HTTP default.

`segment` (default `verbosity="minimal"`) with `action="validate"` returns validity, the
segment's definition and its derived query; `"explain"` adds the rendered SQL; `"preview"`
returns member rows, the preview and member counts, and the derived query. `verbosity="full"`
returns the whole response with compiler plans. Minimal responses still include query and
segment policy effects, warnings, errors, and actionable recovery hints when present.

MCP accepts a `query` object or a JSON string encoding that object. Tool arguments
placed inside it are lifted; conflicting tool options refuse with
`INVALID_QUERY`. Query IR's `verbosity` and `sql_profile` retain inner precedence. The response's `normalized` list reports each spelling change:
`eq`/`equals` → `=`, `neq` → `!=`, `gt`/`gte`/`lt`/`lte` → `>`/`>=`/`<`/`<=`,
and `is_not_null` → `IS NOT NULL`; arithmetic `sub`/`mul`/`div` →
`subtract`/`multiply`/`divide` (`add` is already canonical). Arithmetic `operands`
or `terms` with at least two expressions fold left, including subtraction and
division. Mixing operand shapes refuses. Select dimension shorthand uses the shared
[Query IR rules](QUERY_IR_SCHEMA.md#selectitem): a bare `{dimension: "<id>"}`
moves to `group_by`; an `expression` wrapper moves only when `group_by` is empty
or already lists that dimension. Both shapes accept `kind: dimension|group|ref`.
Aliases and competing targets refuse with `INVALID_EXPRESSION_AST`; a wrapped
dimension beside other grouping dimensions carries `MOVE_DIMENSION_TO_GROUP_BY`.
Each move appears as a `QUERY_SHORTHAND_NORMALIZED` warning with
`details.canonical`, on `execute` in every mode.
Unsupported expression kinds refuse before their children are read. Excessive
expression or predicate nesting returns `INVALID_QUERY` as a structured tool error.
Unknown ids in a dimension-only list query return `OBJECT_NOT_FOUND`, with
closest visible matches.

`execute` accepts either `{"query": {...}}` or Query IR fields at the top level. Metadata tools
accept the same request fields documented in [QUERY_API.md](QUERY_API.md), including optional
`policy_context`.

### Plan

`plan` is the only natural-language intent tool. Its default `detail="query"` returns `status`,
`best.query_ir`, and a `why` or `warnings` entry for any part of the question the draft doesn't
honor; `detail="best"` adds `intent_ir`, `best.trace` and `next`. Forward `best.query_ir` to
`execute` (`row_format="columns"` is the lowest-token shape). When `status="ok"`, the draft has
already paid validation cost, so run `execute` with `mode="validate"` only when you are editing
the IR or need full diagnostics.

Validate or execute `best.query_ir`; `next` carries only `ready_for` and optional
`valid_values` calls, without another copy of the query. In `detail="best"`, fallback
drift reasons point to slot paths in `best.trace.intent_slots` and
`why.details.fallback_slots`. Full/debug detail keeps the expanded slot diagnostics;
query detail keeps them self-contained because it omits the trace.
Other exact repeats use `{"$ref": "best.resolved.0"}` or a `best.query_ir` field path;
follow the dot-separated path from the response root (numbers index arrays).

A draft that validates can still leave out part of the question. `plan` returns
`low_confidence` with `why.code="PLAN_INTENT_COVERAGE_GAP"` when the draft:

- doesn't use a metric the question names by its label, an alias or its id, when that name
  has two words or more and holds every measure the question names ("completed revenue"
  holds "revenue") (`named_metric_unrealized`). `plan` drafts that metric itself, and the
  metric's own name isn't read again as a window, a ranking or a value;
- answers with a measure, or the metric that is its plain aggregate, while a metric that
  reads the same measure through a filter fits the question (`governed_metric_unrealized`,
  the visible metrics in `expected.metrics`). The guard reads the whole question, including
  relative clauses such as "stores that were active last week". It fits when the question
  holds every word of its label (with or without a parenthetical), an alias or its id, in any order and
  with plurals as singulars, or when the measure is a [building
  block](PACKAGE_AUTHORING.md#building-block-measures). A measure authored
  `publish: false` that no metric aggregates whole is not offered to agents:
  `discover` doesn't list it, and `plan` doesn't answer with it unless `partial_query.select`
  names it by id; no other part of the request names it. When a metric reads the measure
  through a filter, it is a building block: `plan` answers with the metric when it is the only
  one that filters the measure or the question names it, and otherwise holds the draft.
  When the question names no governing metric, a published measure is
  still held while a visible metric narrows its rows: an aggregate in that metric, over this
  measure or another one, filters on a dimension of the measure's entity ("New teams" counts
  the creation events of customer teams, so "teams last week" over a count of every team is
  held). A metric's narrowing dimensions are those of the measure's entity it filters on.
  Only when it has none are they the dimensions on entities reached through many-to-one or
  one-to-one relationships that its aggregates of this measure filter on: a class on a
  parent entity ("Calls by paying teams" sums a daily team fact's calls for customer teams,
  so "team calls last week" over every team's calls is held). `expected.metrics` lists up to
  five such metrics, those the question's subjects rank first, and `expected.narrowed_by`
  the dimensions they filter on. A draft that selects the metric, or filters or groups by
  one of those dimensions ("customer teams"), isn't held for it. Hidden governing metrics
  are excluded before candidate selection and diagnostics, and
  their IDs and labels are never named. An unoffered measure with no visible governing metric, or
  a draft whose governing metrics can't be read, is still held, with a generic message and an
  empty `expected.metrics`. A metric hidden only because something it reads, such as the
  dimension it filters on, is hidden still holds the measure it narrows: the message is generic, and `expected` lists only the
  metrics and dimensions the caller can see. A one-measure draft answers with the metric itself when
  it aggregates the measure the same way, the draft's time role equals the metric's own
  `temporal_role`, the subject phrase names no other such
  metric as fully nor the measure more fully, and the draft neither filters nor groups by what
  the metric's filter reads ("demo stores" keeps the measure, held). A measure or metric the
  caller's `partial_query.select` names is never held for this;
- has no filter on a dimension that a "where <dimension> is <value>" clause names, even
  when the catalog declares no values for it (`dimension_filter_unrealized`);
- carries no time window, or a different one, where the question names one
  (`time_window_unrealized`);
- reads a balance (a [stock](PACKAGE_AUTHORING.md#measures) whose key holds a series column besides
  its clock) without time grain `day`, or through a predicate at any grain
  (`stock_as_of_unrealized`). A stock answers with each
  series' last snapshot in each period, so with no time block, or by week or month, a series
  that stopped reporting (a closed account) still adds its last value. The draft is held
  whether it reads the stock through a selected measure, a metric at any depth, a
  `metric_filters` entry. A `metric_predicate` expression or a
  `scoped_aggregate.predicates` entry may read the stock in its own time scope, so an outer
  day grain does not clear that hold. Choose a metric without a stock predicate, or select
  the balance directly. `expected.stocks` lists the visible stocks that require a hold,
  `actual.grain` the draft's grain (`null` with no time block). For a direct balance read,
  ask for one day ("MRR yesterday", `MRR on <YYYY-MM-DD>`), or set `time.grain: day` with
  that day's start and end. A stock keyed by its clock alone (a
  daily rollup) is one series and isn't held. `plan` drafts most balance questions on one
  day itself (see "How plan reads a balance" below); an as-of cue on anything else ("new
  accounts right now") stays `TIME_WINDOW_UNRESOLVED`;
- loses a ranking's stated limit, sort direction or selected measure, cannot identify the
  ranked measure unambiguously, or doesn't group by what is ranked (`ranking_unrealized`),
  including count-free requests such as "top stores by revenue";
- excludes a value requested positively, or cannot prove that its filter keeps or drops
  each named value with the requested polarity (`filter_values_unrealized`). Scalar `=`/`!=`
  and scalar or list `IN`/`NOT IN` can prove it; list-valued `=`/`!=`, empty membership
  lists and pattern filters cannot. The guard intersects top-level filters on the same
  field, accounts for exclusions, and compares draft literals to stored values exactly;
  catalog labels and aliases only identify values named in the question. Nested filter
  scopes do not prove an outer filter's result. Grouping does not cure an uncertain filter.
  Without grouping by the field, the draft returns one total, so its filter must keep only
  values the question names: "revenue for Brooklyn" filtered to Brooklyn and Philadelphia
  is a gap, while "revenue for Brooklyn and Philadelphia" is not. Values an exclusion names
  follow the exclusion rule below;
- combines top-level filters on one field so no value can survive, which returns no rows
  (`contradictory_filters`);
- misses a negation, a prior-period comparison ("vs prior fiscal quarter" included) or one of
  several named subjects;
- counts time in fiscal periods ("fiscal quarter", "FY") on the Gregorian calendar
  (`fiscal_calendar_unrealized`). When the package has one calendar whose name says fiscal,
  `plan` buckets the draft on it itself (`time.calendar_id` with `time.fill: true`) only when
  the question's fiscal words ask for buckets of the draft's grain ("by fiscal quarter",
  "fiscal quarterly") and no to-date or rolling value (`period_to_date` refuses non-default
  calendars). Any other fiscal period ("the first fiscal quarter", "vs prior fiscal year") is
  reported, and its recovery hint asks for the period as exact dates; without such a calendar
  the hint names the package's calendars;
- picked its subject from several that match the question equally well, when neither the
  question nor a `partial_query` select names it (`subject_ambiguous`, with up to five
  candidates in `expected.candidates` and their number in `expected.candidate_count`).
  "revenue" names Revenue over Item Revenue Cents, and "item revenue" the reverse; for
  Gross Revenue and Net Revenue it names neither. `plan` reports every other reason first;
- returns a result without the part the question's shape asks for. "who", "whom", "whose",
  "which" or "list" opening a clause asks for the rows of the entity the clause names: its
  first word outside a time window and a "by" grouping that names an entity or, for "which"
  or "list" with none, the entity a "by" grouping in the clause names ("List revenue by
  store"). The `group_by` needs that entity's declared one-column key (`list_unrealized`, with
  it in `expected.key_dimensions`: "Who ordered last week?", "List customers by month", "Who
  are our customers by store?"). A name may sit beside the key, but never lists the rows on
  its own, since a name can repeat: customers who share one would be one row ("List
  customers" grouped by Customer name, "Which 3 stores had the most revenue last month?"
  grouped by Store name). Nor does any other dimension, whatever it declares: a time grain, a
  category, another dimension of the entity ("Customer order number") or another entity's
  dimension. "which" or "list" naming no entity is held. "who" naming no entity ("Who ordered
  last week?") leaves whose rows to the caller: only a caller's `query.group_by` dimension
  that is no grouping the question lists ("Who ordered by store?") and is the key of its own
  entity lists them. "each" or "every" needs a row per item, a `group_by` or a grain that
  splits the rows (`each_unrealized`: "How many orders did each last week?"). "compare",
  "compared", "comparison", "versus", "vs", "against" or "up or down" needs a value to compare
  with: a prior-period select. A second select (which may spell the first again), a
  `group_by` or a time grain doesn't show what the question compares
  (`comparison_unrealized`: "Were orders up or down last week?", "Compare revenue by store
  last month", "Food revenue vs drink revenue last month"). Two or more questions for a
  value, "how many", "how much", "what is", "what was" or "what's", need a select of their
  own each that names what the question asks about:
  its first words that aren't stopwords, framing words, numbers or a window's words lie where
  the question spells a whole name (label, alias, or the last part of the id or name) of the
  measure, at its declared aggregation, or metric the select is
  (`multiple_questions_unrealized`: "How many orders and how much revenue last week?" with no
  order count select). Selects with one expression count once, and a clock ("Order time"), a
  filter or an expression built on a measure names nothing. A word opens a clause when only
  stopwords, framing words ("show me", "list") or a time window's words come before it since
  the last comma, colon, semicolon, sentence end or "and" ("How many orders last week and who
  placed them?"): in "revenue from customers who are new", "who" asks nothing. Nor does a
  word inside a declared name ("Comparison cost"), and "per" is no such word ("revenue per
  order" is a ratio). `clause` quotes the words, and `actual` gives the draft's
  `select_count`, `group_by` and `time_grain`. `plan` runs this check after every other one,
  the `PLAN_UNMATCHED_TERMS` checks below included, so it holds only a draft nothing else
  holds.

`plan` doesn't answer questions that exclude values yet. An exclusion ("excluding", "except",
"without", "not", "but not", "other than", "apart from", "aside from", "minus", "outside of",
"all stores but") holds whatever the draft carries, the caller's `partial_query` included: each
clause is one gap, `negation_reversed` when a top-level `=` or `IN` filter keeps a value the
clause names, otherwise `negation_unrealized`. Its recovery hint (`ask_for_breakdown`) suggests
asking for the breakdown by the excluded dimension instead ("signups by channel"), which shows
each value and the rows with no recorded value; listing the values to keep would drop those
rows. A time phrase inside an exclusion ("signups not in June 2024") is never read as the
question's window.

An exclusion keeps rows with no recorded value, so its executable form is one top-level `where`
filter per excluded value, `{"field": ..., "op": "IS DISTINCT FROM", "value": ...}`: "signups
excluding web" counts the signups with no channel, which `!=` and `NOT IN` drop. The held
draft uses that form, and a hand-written Query IR with it still runs through `execute`.

The gap's `expected.items` lists what the clause names, each with its `kind`: a declared value
name (its value, label or alias, in double quotes or none) with its `field` and `value`, a
time phrase, or `unknown` for any other word in an item's place. Commas, semicolons, slashes,
"&", "and", "or", "nor", "plus", "as well as", "along with", "alongside", "together with",
dashes, line breaks and brackets separate items; a separator inside a declared name ("Click &
Collect") doesn't split it. The list ends at the first word that is neither; every other
character up to that word is an `unknown` item, except the question's final `.`, `?` or `!`.
The first time phrase after the list with only words between is the question's window
("signups excluding web in June 2024"); any other value, quoted or time mention before the
next exclusion, an "including" or the question's end is an `unknown` item. When an exclusion
word or an "including" falls inside a quoted string or a declared value name ("Including
Top", "All but Web"), or an exclusion word inside a grouping phrase ("revenue by store
excluding Brooklyn"), the whole question is one `unknown` item. An excluded value named "Top"
is never read as a ranking.

`why.details.gaps` names each clause. Question words the draft uses nowhere, other than
framing words (including verbs and function words such as "dated", "placed", "only", "using"),
time phrases the planner read, and numbers the draft carries (a limit, a threshold, the
window's year), come back as a `PLAN_UNMATCHED_TERMS` warning with up to eight of them in
`details.terms`. For the warning, the draft uses a word in the id, name, label or aliases of an
object it uses (a measure's entity and time role included), allowing a plural or one typo, or in
a filter value; descriptions and topics never use a word. A word that names a catalog object is
held to a stricter rule, and one the draft doesn't consume is not a warning: it makes the plan
`low_confidence` with `why.code="PLAN_UNMATCHED_TERMS"`, since the draft dropped a grouping
("by store, customer type and product type" grouped by store; `why.details.dropped_groupings`
names it) or answers about another subject. A word names an object when it is a word of the
object's label or synonyms, or of the last dotted part of its id or name outside the object's own
namespaces ("sales" in `metric.sales.aov_usd` names nothing); a plural counts as its singular.
Only the draft consumes one: by a word of the label or synonyms of an object it selects,
or its id or name (an id the
question spells out whole consumes its namespaces too), a value it filters on or that value's
declared names, a time grain it carries (only its unit and its "-ly" form), a prior-period
shift's trigger phrase, one grouping that names the query's clock at the planned grain (a
second one, as in "by order month and order date", is not consumed), "number of" for a
selected count-valued measure when the draft has no grouping, or a recorded time phrase or
honored clause.
A multi-word authored synonym consumes only the contiguous question span it matches,
allowing regular plurals. Two selectable subjects sharing a whole name or synonym, with
no other words distinguishing them, return `needs_clarification` with both ids; only words
inside a matched name distinguish them. A metric label without its parenthetical counts
toward readiness but never selects a metric: "session to order conversion rate" names both
the `(7d)` and the `(7d, same store)` metric, so it asks which one.
Established whole-name metric precedence over a measure and explicit caller selections
still settle a subject.
At plan entry, straight and curly apostrophe contractions expand before scope, relevance,
parsing and readiness: `what's` becomes `what is` and `didn't` becomes `did not`. Any other
`'s`, quoted text, and a declared name or value spelled with an apostrophe stay as typed.
Messages quote the normalized words.
Stopwords are exempt unless they are exact catalog names. Regular plurals are recognized
and consumed using the same forms; "-es" applies only after s, x, z, ch or sh. A
engine synonym, a typo, a namespace, a description, a framing word or an object the draft doesn't select
(a measure's entity included) never does. So `plan` may hold back a right draft ("revenue from
orders": Orders is a measure), but never calls one ready that drops a grouping the question
names. Last, each grouping the question lists, apart from clock terms and declared values, must
match its own `group_by` dimension, or the plan is `low_confidence` with
`why.code="PLAN_UNMATCHED_TERMS"` and `why.details.dropped_groupings` naming the unmatched ones.
This check reads past a comma when the next piece names a dimension, an entity or a clock, and
reads a window the question states as a comma, while the draft still reads its groupings up to
the comma: "repair cost by incident name, incident" grouped by Incident name alone is not ready,
as two incidents can share a name, and neither is "repair cost by incident name, last month and
incident". It also checks terms after `by` with a comma, tab or newline, after `per`,
`each`, `for each` or `every`, in lists joined by commas, `and`, `&` or repeated clauses, and
the nouns before `by` in `top`, `highest` and `lowest` rankings. Each term records its source
span. Pieces consisting only of connector words add none, and a marker inside a declared name
("Sends per account") opens no clause.
A `level`, `levels`, `grain` or `grains` word outside every declared name holds the plan unless
every grouping the question names is grouped. This check reads which declared names the
question holds, not how the phrase is built, so a plural, a repeated "at" or a separator
changes nothing: "revenue at customer type and at store name levels" needs both Customer type
and Store name. A dimension the question names (its label, the last part of its name or an
alias, as whole words) needs its own id in `group_by`; an entity needs one of the stand-ins
described below. A name doesn't count inside a longer declared name ("customer type" is not
also the entity Customer) or inside a phrase naming the query's clock ("order date"), and
neither does a declared value or a dimension the draft's `where` pins to one value (`=`, or `IN`
with one value). The word before each level word must end the name of a dimension, an entity
or a clock: "revenue at region level" with no Region is not ready, nor is "revenue at the
level". A declared "Severity level" dimension or "Stock level" measure triggers nothing. Some
complete plans are held on purpose: in "revenue at store level for customer types new and
repeat", "customer types" is no declared name, so the entity Customer must be grouped.
These readers add obligations only to the dropped-grouping check; planning and the checks that
authorize a draft's groupings retain their existing readers. The check only holds a plan; it
never changes a draft or makes one ready.
In any question, with a level word or not, every caller-visible dimension name the question
holds has the same obligation, whatever words surround it. An entity name has it in a level or
grain question, where one of its stand-ins must be grouped; elsewhere an entity name often
describes the measure ("repeat-customer orders"). Names are read as above and also with
underscores, spaces and any case: `customer_type` and `customer type` name Customer type, and
declared leading and trailing underscores are kept, so aliases such as `_customer_type` and
`customer_type_` count too. The draft must group by the dimension, or pin it to one value with
a query-level `where` filter. A selected metric's own filter restricts only its aggregate and
does not discharge a named grouping. A value word inside the name
never discharges it, and neither do words the selected measure's label shares: with an alias
`_new_type` for Customer type and revenue labelled "Revenue (new and repeat types)", "revenue
by _new_type and store name" grouped only by Store name is not ready. A name holding a value
that a draft filter keeps (`=` that value, or `IN` with it) holds the plan even when the draft
groups by it, since the filter may narrow the answer. `why.details.filter_inside_grouping` lists
each such filter as `{"term", "field", "value"}`, `dropped_groupings` keeps the term, and the
recovery hint asks the caller to confirm the value with the user or remove it from that field's
filters in `best.query_ir.where`. This reading never reads a hidden dimension, entity, measure,
metric, temporal role or calendar, so a hidden object answers exactly as an absent one: a hidden
clock is no clock, and its words keep their obligation. It only holds a plan; the draft and its
filters are unchanged.
A listed grouping that names an entity is satisfied only by that entity's own key
dimension, or by the single declared dimension of that entity whose own words name it, and an
entity with a composite key is never satisfied. A term names an entity only with every word of
its label ("customer" names Customer, not Customer history), and a declared time, such as Store
opened at, never stands in for its entity. Any other grouping matches a dimension whose own
words name it: its label, its aliases and the last part of its name, not the prefix of its id.
A grouping that dimensions of two or more entities match, none of them the measure's own
("name" for an order count: Customer name or Store name), is ambiguous and never a pick: the
plan is not ready, and `why.details.ambiguous_groupings` lists it. Naming the entity ("customer
name") settles it, as does a dimension in the caller's `partial_query` group_by when the draft
adds no other that matches.

The reverse also holds: every grouping the draft adds traces to the question, or the plan is
`low_confidence` with `why.code="PLAN_UNASKED_GROUPING"`, `why.details.unasked_groupings`
naming each one (a dimension's label, or the grain's unit), `details.dimensions` and
`details.grain`. A `group_by` dimension traces to a grouping the question asks for, read as
above: one it lists ("by store"), the noun a ranking ranks ("which 5 stores had the most
orders"), or the words after "per", "each" or "every" ("revenue per store"). It also traces to
the caller's `partial_query` group_by, or to the draft's own `=` or `IN` filter, which keeps only
values the question names. The time block's grain traces to the question's words outside its
windows: its unit or "-ly" form ("by month", "monthly", "at month level", "daily"), a series
("over time", "trend", "trending", "time series"), or for days a grouping that names the
query's clock ("by order date"). It also traces to the caller's `partial_query` time grain, or
it can't split the rows because the window fits in one bucket of the grain ("in Q1 2017", "last
month", "yesterday"). Packages declare no default grain, so the month plan picks for a
comparison ("food revenue vs drink revenue by store"), a year-over-year shift ("revenue vs last
year") or a qualified ranking, and the unit of a window of several periods ("revenue last 7
days" by day, "revenue by store in the last 3 months" by month, "revenue in 2016 and 2017" by
year), are held: name the grain ("monthly revenue for the last 3 months by store", "revenue in
2016 and 2017 by year") or follow the `remove_unasked_grouping` hint.

A ranking (a draft with a `group_by`, a `limit` and a first `order_by` on a selected value) must
also keep the top N of the entity the question ranks. It ranks the entity when the ranked noun
("top 3 stores", "which 3 stores") is not a time unit and reads every `group_by` dimension, as
above (an entity's key and its label). When the question ranks nothing, a ranking the caller's
`partial_query` states (its `limit`, over its own `group_by`) traces to it unless a grain splits
its rows. A ranking of the entity whose rows a traced grain splits ("top 3 stores by revenue at
month level", "top 3 stores by monthly revenue") would keep the top 3 store-months, so it is
held with `why.code="PLAN_RANKING_PERIOD_AMBIGUOUS"`; its message asks which ranking the
question means, the top 3 stores over the whole window or the top 3 stores in each month. Any
other ranking whose rows aren't the ranked entity's, split by a grain or not ("which 3 stores
have the highest revenue by customer type" keeps the top 3 store and customer type pairs, a
ranked period such as "which 3 months had the highest revenue by store"), is held with the same
code. Every such hold has no `clarification` and no Query IR in `why.details`: as its
`ask_which_ranking` hint says, ask the user which ranking they mean and plan again with a
question that names it. Both checks only hold a plan; neither changes a draft or makes one
ready.

Words that name no catalog object also make the plan `low_confidence` when the draft
doesn't consume them, they aren't stopwords or number words, and `intent_ir.unresolved`
still holds them. This returns `why.code="PLAN_UNMATCHED_TERMS"` with
`why.details={"terms": [...], "kind": "filter_values_unrealized"}` and an
`add_missing_condition` hint: find values with `valid_values`, add the filter, then validate,
or ask the user what the words mean. No hint offers to ask again without a word a hold names
(a catalog name, a grouping, a number or an unknown word), since the question without it may be
another one, except the "s" ending a contraction or possessive that plan reads as an unknown
word ("What's revenue last month?"): every check reads that retry again. No other ending is
offered: without its "t", "can't" says the opposite. A single unknown
value such as "Brooklyn" blocks readiness when the package declares no value domain for it; plan never guesses its dimension or queries
the warehouse to resolve it. Other unmatched words stay warnings; check them before executing.
A number, or a clock or zone word, the draft doesn't carry is not a warning: it makes the plan
`low_confidence` (below), since the draft dropped an hour, a range or a
threshold. Every measure a
question lists ("item revenue and orders in Q1 2017") is in the draft's select list or the
plan is `low_confidence` with a `multiple_subjects_unrealized` gap naming the ones it left
out. For a measure by a dimension, a measure the question names in full outranks a shorter one
it shares a word with ("item revenue" is Item revenue, not Revenue), but only when the name
holds every word of the measure the question otherwise asks for: "large order revenue" is
still revenue (and `low_confidence` until the draft filters on large orders). If a
validating fallback would change the target, grouping, qualification/cohort,
filters, or time scope, `plan` returns `low_confidence` with
`why.code="PLAN_FALLBACK_SEMANTIC_DRIFT"` instead of silently promoting it.
`plan` resolves a time window only when the question names exactly one, in a form it reads
unambiguously: a year after "in", "for" or "during", or after the word "year" where no word
qualifies it ("year 2017", "the calendar year 2017"; "financial year 2017" and "model year 2017"
are not calendar years and are reported), consecutive years, a quarter or half with a year ("the first half
of 2017", "H2 2017"), a month or month range with a year, days with a year ("March 1 to March
31, 2017", "Mar 1 - Mar 31 2017"), an ISO date or ISO range ("2017-03-01 to 2017-03-31"), or a
relative window ("last 7 days"). A single named month, quarter or half without a year
("in September", "Q3", "the first half") uses the latest such period starting on or before
`policy_context.now` (the package's time zone, or UTC), and `assumptions` names the year.
Without a day, every month name needs a scoping word ("in", "for", "during", "on" or
"since") or must be the whole input: "customer April", "Jan's revenue" and "the June
promotion" do not resolve a month. "First half hour" and "first quarter hour" do not name
calendar periods.
Only a completed period resolves: if it contains the reference date, `plan` returns
`needs_clarification` with `TIME_WINDOW_UNRESOLVED` and `why.details.possible_readings` naming
that period through the last complete day and the same period a year earlier. A month and
day without a year ("Sept 30", "September 1st") uses the latest such date; an optional weekday
("Wed Sept 30") must match it, otherwise the same clarification names the stated weekday
and the date's actual weekday. "Since September", "since Sept 22" and "since Q3" start at
that inferred date and end at the start of the reference day, so they include only complete
days. "Early", "late" and "mid" months and multiple named periods remain unresolved.
"All time", "of all time", "ever", "in total", bare "to date", "since launch", "since the
beginning" and "since we started" record their spans and add no start or end, with the
assumption "all time: no start date". A caller's bounded window cannot silently narrow them.
Beside exactly one bounded window, "ever" and "in total" instead emphasize that window
and add no all-time assumption ("signups in total last month"). The other all-time forms
still conflict with a stated bounded window. "Since launch of …" and "since the beginning
of …" are not all-time readings. A named or all-time phrase inside an exclusion ("not in
June", "excluding Q2", "not on Jun. 25") follows the exclusion rule above: it never becomes a
positive window, and `plan` offers no readings for it.
A balance such as "MRR of all time" remains held: a stock needs an as-of day. Period-to-date
forms ("year to date", "month to date", "ytd", "mtd") retain their existing handling. Planning
reads no warehouse data to establish coverage; the all-time assumption makes no claim about
the first date with data. A range's spoken end is included: the response's
`assumptions` says so, with the exclusive `time.end` it chose. A window restated right beside
itself ("Q1 2017 (January 1 to March 31, 2017)") is one window; two that differ, or the same
one beside another condition ("revenue in 2017 from customers who signed up in 2017"), are a
conflict, and `TIME_WINDOW_UNRESOLVED` names both in `why.details.conflicting_phrases`. `plan`
resolves days and coarser windows only. A window shorter than a day ("last 24 hours", "past
hour", "last 30 minutes") returns `TIME_WINDOW_UNRESOLVED` with `why.details.sub_day_phrases` and
no `best.query_ir`, never a query over all time. Every other hour or zone is caught by one
rule: a draft is `ok` only if every number, number word and clock or zone word in the question
is consumed by a construct the draft carries. The words it counts are numerals ("9", "14h30",
"1930"), spelled-out numbers ("nine", "twelve", "twenty", "hundred", "half", and "quarter" in
"quarter past" or "quarter to"), "o'clock", "hour", "minutes", "noon", "midnight", "morning",
"UTC", "GMT", a zone code ("EST", "PST", "CET", "AEST", "MSK", "WIB" in any case, and "ET", "PT",
"CT", "MT", "Z" in capitals, so an all-caps state code such as "CT" is read as a zone) and a
name such as "Europe/Berlin". A word is consumed only where it sits inside the text of a
construct, not because its value equals something the draft holds: the words of the date or
window plan resolved, the count of the ranking that states the draft's limit ("top 5", "the 5
customers who spent the most", "3 stores with the highest revenue"; a threshold that repeats
the limit's number, as in "top 10 stores with at least 10 orders", does not consume it), the number of a threshold or
percentile the question states ("over 12.50", "90th percentile", "1,000 or more"), a filter
value, or the name, label or alias of an object the draft selects (not its description). A number
counts as a percentage only when "%", "percent" or "percentile" follows it: "50 percent" states
0.5, while "500" never states 5. So a "1930" or "2000" that no such text holds is left over, and so is a "9"
that a limit of 9 does not state. Otherwise the plan is `low_confidence` with `why.code="PLAN_UNMATCHED_TERMS"`, the leftover words in `details.terms`
and no `next.ready_for`. So "between 9 and 17 on 15 March 2017", "from nine to five", "at
14h30", "at 2000" and "in UTC" are not ready, and neither is a number range plan doesn't read
("aged 25-34", "2 to 5 orders") or a token that is not one number ("15.03.2017", "1.2.3",
"10.0.0.1"); a number the draft does carry ("top 10") is fine. A zone
written as an ordinary word ("Pacific time", "London time", "local time") is not recognised by
itself, so with no hour beside it the question reads as its day. A window in the draft
(one you pass in `query.time`, or plan's own) consumes the date phrases plan resolved only if it
agrees with them: it carries both bounds, the earliest start and the latest end of the windows
the question states ("15 March 2017" against 15 to 16 March agrees; against 1 June 2018, the
whole of 2017, or a start of 15 March with no end, does not). Plan reads a window you pass only
when it carries both bounds, each a date or 00:00 with no offset or a UTC one, and a bound with
a zone designator only when that is the role zone's offset at that instant: a missing bound, a
bound with another time of day, or a window whose start is not before its end, is held ("15
March 2017" against 12:00 to 13:00 on that day). If it disagrees, the draft is
`low_confidence` (`PLAN_INTENT_COVERAGE_GAP`, gap `time_window_unrealized`) and the phrase's
numbers are left over. A window you pass in `query.time` is not held to a lone "previous
month" when the draft carries a `prior_period` expression: that phrase is the comparison's offset,
not a window. The window also
answers a phrase plan could not resolve ("last 24 hours", "before 2017"). A bare year is never
consumed because a window's bounds hold it, its exclusive end year included: "revenue 2018" or
"at 2000" is left over whatever the window says, and a year is part of a phrase plan could not
resolve only when it follows a bound or qualifier word ("before", "until", "of"). In a question over 2,000
characters plan reads no window, so only the 20xx years after "in", "for", "during" or "year" are
checked, as calendar years, and only when they name one year: a count ("in 2000 or more") is not
one, and two different years ("in 2017 ... for 2000 customers") cannot be told from a count, so
no window is read and both years are left over. It never consumes a time of day,
an hour or a zone, whatever hours its bounds carry: a question that states "12:00 to 13:00" or
"noon" is never ready, even against a window with those hours. To ask for an hour range, pass it in `query.time` yourself, as
end-exclusive ISO timestamps in the temporal role's time zone (the role must be a timestamp),
for example `start: "2017-03-15T12:00:00"`, `end: "2017-03-15T13:00:00"`, with the role and
grain, and plan a question that names neither the hours nor the day ("orders"): those bounds
are not the whole day a date phrase states. "and" joins a range only after "between": "between March and May 2017" is
a range, while "March and May 2017" names two months. Unsupported calendar forms, such as a
bound ("before 2017", "since March 2017"), a qualifier ("early 2017"), a comparison ("2017 vs
2016", "2017 over 2016"), a numeric date (4/3/2017), two periods joined by "and", or two
windows at once (such as "last month and this month"), return `low_confidence` with
`why.code="TIME_WINDOW_UNRESOLVED"` and the phrases they couldn't resolve, rather than the
nearest parsed window. In a question that names fiscal periods, only exact days resolve (days
with a year, ISO dates, and "last 7 days", "yesterday" or "today"): "fiscal Q2 2017",
"FY2017", "last fiscal quarter" and even "in 2017" are unresolved, since only the package's
fiscal calendar can date them. That response has no `best.query_ir` to execute, since a query
without the window answers a different question: pass the window in `query.time`, with the
temporal role and grain, and plan again. **Qualified relative periods remain a limitation:**
for phrases such as
"before today", "after last month", or "until this week", `plan` may return `status="ok"`
with the embedded period's bounds but without the qualifier, rather than
`TIME_WINDOW_UNRESOLVED`. A `PLAN_UNMATCHED_TERMS` warning may appear, but "until" is treated as
a framing word, so "until this week" can have no qualifier warning. Regardless of warnings,
compare `best.query_ir.time` with the intended bounds or provide explicit `query.time` before
executing such a draft. A total over a window gets
one bucket when one calendar grain holds the window; an explicit grain ("monthly") wins. When a
draft can't take the window's start, because the metric looks back over earlier periods or the
question compares with an earlier period, `plan` keeps the end and returns
`why.code="TIME_WINDOW_START_DROPPED"` with the start to filter by.
Questions longer than 2,000 characters are not partially parsed for time: unless the caller
provides a complete window in `query.time` (both `start` and `end`, or a relative `range`),
they return `TIME_WINDOW_UNRESOLVED` with a request to shorten the question or supply those
bounds. Include the selected `temporal_role` and `grain` in that time block. A date or
qualifier beyond the limit therefore cannot silently disappear from an otherwise ready draft.
**How plan reads a balance.** When every select reads a balance directly (a stock measure, or
a metric that is one aggregate of it, on the stock's `as_of_time` clock), `plan` drafts it on
one day: the last complete day before `policy_context.now` in the clock's zone when the question
has no time words or says "now", "right now", "currently", "current" or "at the moment"; the closing
day of the period an "end of" or "as of" phrase names ("at the end of last month" on 2026-10-05
is 2026-09-30); a day the question states; or the closing day of one stated period ("MRR last
month"; the opening day for a `start_of_period` stock). The draft carries `time.grain: day` on
that clock with the day (`range.last` of one day for the last complete day), and `assumptions`
names the day. A generated building-block stock measure is answered with the metric governing
it, as for any draft on that metric's clock; a caller's select, expression and alias are kept.
A generated governed balance must be named in full: "pro accounts" cannot stand for "Paying
accounts". Time words without a single-day reading ("all time", "ever", "to date", "since
launch", "trend") keep the `stock_as_of_unrealized` hold. A day that isn't complete (today,
the end of this week) isn't ready to execute, and no earlier day stands in for one. A
day-grain balance window is read only when both bounds are whole days and it ends on or
before the last complete day; otherwise `stock_as_of_unrealized`. This includes a caller's
`query.time`; the gap hints at the last complete day. When that day has no rows,
`execute` returns none with `EMPTY_RESULT_WINDOW`. When a `metric_constraint` requires the
clock's date dimension in `group_by`, `plan` adds it (beside the day grain it adds no row) and
counts it as asked; any other required field, or one hidden from the caller, keeps the
`POLICY_DENIED` hold. A balance the question compares ("MRR right now compared with a week
ago"), or asks for by week, month, quarter or year where the clock or a constraint reads it per
day, returns `needs_clarification` with `next.action: "clarify"`, a `stock_as_of_unrealized`
gap and `why.details.clarification`. Several periods ("MRR last 3 months", "by week" where
weeks are allowed), a stock on an event clock, a ratio, or a balance beside a flow keep the
holds above, and a window in `query.time` is used as passed: a day-grain balance window is read
only when both bounds are whole days and it ends on or before the last complete day;
otherwise `stock_as_of_unrealized`.
A select item the caller passes in `query` appears once, under the caller's alias (the draft's
`order_by` follows it); a list field that isn't a list, or a `group_by` entry that isn't a
dimension id, returns `INVALID_QUERY` with the path and a recovery hint.
Use `detail="full"` only when you need alternatives or blocked drafts.

### Query Verbosity Tiers (execute modes)

`execute` defaults to `verbosity=minimal` in every mode. `query.verbosity` takes precedence
over the outer `verbosity` argument; error envelopes (`ok: false`) use the same resolved
verbosity as the runtime. This is an MCP-only default —
the HTTP `/api/v1/*` default remains `compact`.

| Verbosity | What's kept | When to use |
|---|---|---|
| `minimal` (MCP default) | Outcome, errors and warnings; mode `sql` adds `rendered_sql`; mode `run` adds rows and counts | Answering a question |
| `compact` | Adds query metadata, output columns and the semantic trace; excludes `explain` and logical, physical, performance, fanout and SQL plans | Reviewing how an answer was formed |
| `full` | Adds compiler plans inside `explain`, subject to the same response budget | Debugging |

The MCP adapter never repeats `logical_plan` at the top level. Full responses can include it
inside `explain`. To review relationship paths before running a cross-entity query, call
`execute` with `mode="sql"` and `verbosity="full"` and read `explain.chosen_paths` when that
optional detail fits. `omitted_fields` names detail removed by verbosity or budget shaping.

`sql_profile="off"` drops `rendered_sql` and `sql_plan` at any verbosity for callers that want the semantic envelope without the SQL.

`execute` also accepts `row_format="columns"`. The default `row_format="records"`
keeps `rows` as objects (`[{...}]`). The opt-in columnar form returns
`columns: [...]`, `rows: [[...]]`, `row_format: "columns"`, and the same
`row_count`, warnings, and errors while avoiding repeated field names.

In mode `run`, `execute` returns at most `max_rows` rows (default 200, up to 100,000). A larger
result comes back with `truncated: true`, `total_row_count` and an `EXECUTE_ROWS_TRUNCATED`
warning that says how to narrow the query. Execute asks the warehouse for up to 10,000 rows (or
`max_rows`, if larger) to count them, so `total_row_count` is `null` when more rows exist than
were fetched. Some warehouse adapters fetch the whole result and then clip it; the cap bounds the
response, not the warehouse work.

A `limits.max_rows` inside the query is an operator's fetch ceiling. It can lower the `max_rows`
cap (and then `total_row_count` is `null` once it is reached), but it never raises it.

Every tool and mode shares one response budget: `MCP_DEFAULT_MAX_RESULT_CHARS`, default
32,000 characters of the final compact JSON payload. Both `structuredContent` and
`content[0].text` carry that same payload. The check runs after unknown-argument warnings,
trusted request context and session annotations are added. It measures each representation's
payload, rather than the JSON-RPC wrapper or the sum of the two copies.

Optional compiler plans are removed first, then rendered SQL in mode `run`, then metadata
and query/context echoes if needed;
`omitted_fields` lists what was removed. Compact responses exclude plan detail even when it
would fit. Rows are shortened only by the row cap, never silently by the character budget.
Rendered SQL remains required in mode `sql`. If required data (rows, SQL-mode text,
a Query IR draft or diagnostics) still cannot fit,
the tool returns a bounded `RESULT_TOO_LARGE` error. No partial rows or SQL are returned.
Narrow the query, lower `max_rows`, select fewer columns, or request fewer catalog objects.
Session hints are added only to responses that fit; their size is checked again before
delivery. Only a final successful run carrying rows is recorded for `already_ran` advice.
An operator changes the shared budget with `SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS`, read on
every call. Missing or non-positive values use the default; positive values below 512 use
512 so the refusal itself fits. Refusals preserve request identity and context when they fit.

The `query` that execute echoes back carries the caller's own `limits`; a transport-level
`max_rows` does not become part of that query. The HTTP `/api/v1/query` endpoint leaves
the response uncapped unless the query itself sets a limit.

Query patches returned by `discover` and `inspect` contain only Query IR fields and validate as
returned. They never carry the caller's `policy_context` or the tool's own arguments, so pass the
policy context again on the call that uses a patch. A patch that selects a metric needing a time
window carries the metric's default one. These tools read Query IR only from their `query`
argument, not from Query IR fields passed at the top level.

### Semantic Trace

`plan.best.trace` (`detail="best"` and above) and `execute` with `verbosity="compact"` or
`"full"` expose a compact, human-facing explanation of what the runtime did.
The trace includes intent slots, selected subjects, filters, groupings,
relationship paths, root entity, rewrite/fanout status, fallback decision, and
whether SQL is present. It intentionally omits full physical plans and is not
stored server-side.

### Soft-fail warning codes

Tools surface non-blocking signals in the top-level `warnings` array — read it before concluding a response was silent. The codes the adapter emits:

| Code | Tool(s) | Meaning |
|---|---|---|
| `DISCOVER_IDS_TRUNCATED` | `discover` | Empty `terms` listed one page of ids and more remain; `details.next_offset` is the next page |
| `DISCOVER_TERMS_COERCED` | `discover` | `terms` was a non-string (int/float/bool); coerced to a string |
| `<TOOL>_UNKNOWN_ARG` | every tool but `segment` | Unknown argument (on `discover`, incl. `term`/`kind` typos); the value was ignored |
| `VALID_VALUES_NO_DOMAIN` | `valid-values` | Dimension has no declared value domain: `ok: false`, `status: needs_live_query`, and `next_call` gives the exact opt-in `valid-values` call with `allow_live_query=true`. No warehouse lookup runs by default; a declared empty domain remains a successful empty result |
| `EXECUTE_EMPTY_RESULT` | `execute` | Returned 0 rows with no user filters — verify the measure/time range |
| `PLAN_UNMATCHED_TERMS` | `plan` | The draft uses none of `details.terms` — check it answers the question before executing. As a `why` (status `low_confidence`, no `next.ready_for`) when one is a number or a clock or zone word, when one names a catalog object, when a listed non-clock, non-value grouping has no matching dimension of its own (`details.dropped_groupings` lists only unmatched terms) or may be a dimension of any of several other entities (`details.ambiguous_groupings`), or when two or more are names the catalog doesn't have |
| `EXECUTE_ROWS_TRUNCATED` | `execute` | Returned `max_rows` of `total_row_count` rows — narrow the query or raise `max_rows` |
| `UNGRAINED_TIME_PROJECTION` | `execute` | From the runtime: an ungrouped query has a temporal role but no grain and no `start`/`end` window, so rows group by the raw timestamp — set `time.grain` |
| `UNGRAINED_GROUPED_TIME_PROJECTION` | `execute` | The same for a grouped query: each group returns one row per distinct timestamp. Same shape, with a `SET_TIME_GRAIN` recovery hint |
| `NO_DATA_YET` | `execute` | Guarded `NULL` series buckets beyond visible coverage, empty totals with coverage proven before the window, or no visible coverage. `details.outputs` lists output aliases; `details.measures` lists `{id, edge, edge_source}`. `last_bucket` names the SQL bucket key (date for day or coarser grains, full ISO timestamp for sub-day grains); `before_window` names the resolved window start. Both `edge` and `edge_source` are null with no visible coverage. Empty series with dated coverage keep existing warnings. Replaces `EMPTY_RESULT_WINDOW`; covered outputs are excluded from `NO_DATA_IN_SCOPE`. Granted only when every named measure is granted. See [Empty groups](QUERY_IR_SCHEMA.md#empty-groups-null-or-0) |
| `NO_DATA_IN_SCOPE` | `execute` | A sum, count or distinct count (or a sum or difference of them) read `NULL` on every returned row (or nothing came back and neither a `start`/`end` window nor a metric filter explains it): its measure has no data in this query's scope, so it is `NULL`, not `0`. `details.outputs` names them; check the filter values. Under `observation_scope: "dataset"` an empty answer to a filtered query never gets it. See [Empty groups](QUERY_IR_SCHEMA.md#empty-groups-null-or-0) |
| `FILTER_VALUE_NOT_FOUND` | `execute` | Under `observation_scope: "dataset"` (the default): a string `=` or `IN` `where` value matches no row of its dimension that the caller can read, so its 0 may be a misspelling. Aggregate-filter literals of a retained additive series are checked in both observation scopes. One warning; `details.filters` lists each `dimension`, `value` and closest `suggestion`. See [Empty groups](QUERY_IR_SCHEMA.md#empty-groups-null-or-0) |
| `MIXED_TIME_ROLES` | `execute` | With no `time` block, the selects read measures of different entities or governed metrics with differing sets of real time roles, mixing at least two distinct roles. Undated measures are ignored; a governed metric counts as one clock. Each period is read on its own role's clock, and measure-level filters can bound those periods. The message names the roles, and `details.clocks` lists them. See [What an answer covers](QUERY_IR_SCHEMA.md#what-an-answer-covers) |
| `FILTERED_SERIES_BUCKETS_DROPPED` | `execute` | An unsupported filtered additive series omitted observed buckets. `details.dropped_buckets` lists their time and grouping keys from a separately authorized source query; an average over returned rows omits them |
| `FILTERED_SERIES_BUCKETS_UNVERIFIED` | `execute` | Missing observed buckets could not be established because the source query was denied, failed or capped, or the answer has a limit or population filter. `details.reason` explains why |
| `QUERY_SHORTHAND_NORMALIZED` | `execute` | A select item was accepted as shorthand and rewritten; `details.canonical` is the form to send next time (`plan` accepts the same shorthand but returns the canonical form in `best.query_ir` instead of a warning) |
| `SEMANTIC_CAVEAT_APPLIED` | `execute` | Package-authored advisory context matched the query; interpret affected results with that context |
| `SEMANTIC_CAVEATS_TRUNCATED` | `execute` | More caveats matched than this verbosity returned; increase verbosity to inspect the rest |
| `ROUTE_COLOCATED_KEY`, `ROUTE_RECORDED` | `execute` (`compact`, `full`) | Info: an entity pair the query reads has two or more routes, and the engine used the start's own key or the package's recorded routes; `details.route` is the route, `details.alternatives` (own key) the row for each other route that would load beside the package's rows, `details.conflicts_with` any other route with the rows its row would disagree with, `details.rows` (inherited) the rows it follows |
| `ROUTE_PASS_THROUGH` | `execute` (every verbosity) | The package's one route for a pair the query reads passes through another table's rows that the package doesn't declare as a link table, so a start row with none is left out and one with several can count under several targets; the message gives the meaning and the fixes, `details.through` the crossed entities and `details.fixes` the declarations that settle it |
| `NULL_PRESERVING_HISTORY` | `execute` | A dimension the query groups or filters by is read through a hop into a validity window; grouped, the warning counts the answer's empty group from the returned rows, or is dropped from a complete answer without one |

Every `*_UNKNOWN_ARG` warning carries `details.received` (the offending key). Most also carry `details.closest_matches` (up to two ranked suggestions via `difflib.get_close_matches`); the special-cased singular/plural typos (e.g. `term` → `terms` on `discover`) carry `details.expected` with the canonical spelling instead.

`discover` reads `kinds` from an array, a comma-separated string, or a JSON array in a string (`"[\"metric\"]"`). A value that does not parse, or names a kind the call cannot produce (`details.unknown_kinds`, with `details.valid_kinds`), is refused instead of returning an empty result: with `INVALID_MCP_ARGUMENTS` (over HTTP too, except that HTTP reports a value that does not parse as `INVALID_REQUEST`). With `terms` the ranked kinds are `measure`, `metric`, `segment`, `dimension`, `entity` and `dimension_value`. The id listing (empty `terms`) exists on MCP only and accepts the catalog kinds instead (which include `temporal_role`, `relationship` and `value_domain` but not `dimension_value`); over HTTP, empty `terms` run the ranked search and take the ranked kinds. In resource-grant mode only `metric`, `dimension` and `temporal_role` are produced: any other kind is refused with `valid_kinds` naming those three, on a ranked search over MCP or HTTP and on the MCP id listing alike (the shared `/catalog` and MCP catalog resources keep their usual shape under a grant), and a grant search never reports `no_matches`. So "No semantic objects … matched" means the search ran over the requested kinds (the response's own `no_matches` field); a search screened out before it ran (`low_relevance`, `out_of_scope`) never says it, and a `limit` below 1 is refused over MCP and HTTP. A misspelled `kind` argument is ignored with `DISCOVER_UNKNOWN_ARG`, and the recovery hint says the filter was not applied.

Errors return `INVALID_MCP_ARGUMENTS` (with `closest_matches` for typo'd keys) when the boundary contract is violated outright — e.g. wrong arg name, wrong type, value outside a declared enum. The full envelope shape is identical across all six tools.

### Argument strictness contract

No tool silently accepts unknown keys. `segment` **rejects** them with `INVALID_MCP_ARGUMENTS`; `discover`, `inspect`, `valid-values`, `plan` and `execute` **warn** and ignore them with `DISCOVER_UNKNOWN_ARG`, `INSPECT_UNKNOWN_ARG`, `VALID_VALUES_UNKNOWN_ARG`, `PLAN_UNKNOWN_ARG` or `EXECUTE_UNKNOWN_ARG`. The warn-and-ignore tools cannot reject all unknown keys because callers legitimately add `policy_context` and may pass canonical Query-IR keys (`select`, `time`, `version`, etc.) at top level on `execute` — those passthroughs are part of the contract and never trigger an unknown-arg warning. Query-IR shape errors (e.g. an unknown key *inside* `query`) surface separately as `INVALID_QUERY` from the IR validator in `ast.py`.

Unknown tool names return `UNKNOWN_MCP_TOOL` with `details.available_tools` listing the
current package's tools. `initialize` reports `v2` as `serverInfo.version`, responses carry it
as `api_version`, and `mcp doctor` prints it.

## Resources And Prompts

Declarative resources:

- `semantic-rails://capabilities`: the interface version (`v2`) and complete tool definitions,
  resources and prompts. Existing consumers can read `tools[].inputSchema` and `outputSchema`.
  This is a large resource; `tools/list` also has the tool definitions.
- `semantic-rails://capabilities/summary`: a small opt-in index of tool names and titles,
  resources and prompts.
- `semantic-rails://catalog/summary`: descriptive rows (up to 200 per kind) and `counts_total`.
  Existing consumers can read fields such as `catalog.measures[].id`. This is a large resource.
- `semantic-rails://catalog/index`: a small opt-in index of counts and ids per object kind,
  the same as `discover` with empty `terms`, unpaged.
- `semantic-rails://catalog/full`: every object's full card and the alias index. It grows with the
  package (about 200K tokens for `jaffle_shop`), so read the index first. `resources/read` has
  no paging arguments; paging this resource needs resource templates, planned with the
  `2026-07-28` work below.

Declarative prompts:

- `semantic-rails-query-builder`
- `semantic-rails-query-review`
- `semantic-rails-segment-workflow` (only when the current package defines segments)

Use `adapter.read_resource(uri)` and `adapter.get_prompt(name, arguments)` to access these
surfaces in process.

## Packaged Server Commands

For a UV-installed local package, use the installed console script and an
absolute package path derived from the project you created with `init`:

```bash
PACKAGE_PATH="$(pwd)/my_package"
semantic-rails mcp setup --path "$PACKAGE_PATH"
```

From a source checkout, prefix commands with `uv run`; `--package jaffle_shop`
loads the bundled synthetic fixture package:

```bash
uv run semantic-rails mcp stdio --package jaffle_shop
uv run semantic-rails mcp doctor --package jaffle_shop
PACKAGE_PATH="$(pwd)/my_package"
uv run semantic-rails mcp stdio --path "$PACKAGE_PATH"
uv run semantic-rails serve --package jaffle_shop --host 127.0.0.1 --port 8091
```

`mcp doctor` loads the package and adapter once, confirms the required tools are
registered, and prints the exact stdio/http commands to run next. It does not
bind a port.

The `serve` command runs a foreground server for both the HTTP API and
stateless Streamable HTTP. Start it in a separate terminal and stop it with Ctrl-C:

```bash
semantic-rails serve --path "$PACKAGE_PATH" --host 127.0.0.1 --port 8091
```

Local MCP clients can launch stdio with `mcp setup --install --yes`, on every
supported platform. The raw stdio command stays open until its client disconnects:

```bash
semantic-rails mcp stdio --path "$PACKAGE_PATH"
```

Casual local setup:

```bash
semantic-rails mcp setup --path "$PACKAGE_PATH"
semantic-rails mcp setup --path "$PACKAGE_PATH" --client both --mcp both --install --yes
semantic-rails mcp setup --path "$PACKAGE_PATH" --client claude-code --mcp query --install --yes
semantic-rails mcp setup --path "$PACKAGE_PATH" --client cursor --mcp query --install --yes
```

`mcp setup` checks that the package loads, confirms the MCP adapter exposes the
expected tools, and previews or installs stdio entries for one client:
`--client claude` (Claude Desktop), `codex`, `claude-code` or `cursor`. `both`
means Claude Desktop and Codex. Run it from inside the package directory, pass `--path`, or set a local
profile with `semantic-rails profile init --package-path ./my_package`.

Lower-level client config helpers:

```bash
semantic-rails mcp client-config --path "$PACKAGE_PATH" --client both --mcp both
semantic-rails mcp client-config --path "$PACKAGE_PATH" --client claude --mcp both --install --yes
semantic-rails mcp client-config --path "$PACKAGE_PATH" --client codex --mcp both --install --yes
```

`client-config` writes stdio MCP entries. For Claude Desktop it updates
`claude_desktop_config.json`; for Codex, `~/.codex/config.toml`; for Cursor,
`~/.cursor/mcp.json`. For Claude Code it runs `claude mcp add-json --scope user`
for each server, replacing a user-scope server of the same name; a local or
project server with that name still takes precedence in its project. Each
install keeps the file's other servers.

**Claude Desktop:** quit it completely (Quit, not closing the window) before
`--install`, then start it. It writes its configuration back when it quits, so an
edit made while it runs is lost and the old server keeps answering.
**Claude Code:** use `--client claude-code`; it registers the server with
`claude mcp add-json`, live in the next session.

Use `--mcp query`, `--mcp architect`, or `--mcp both` depending on whether the
client should answer governed analytics questions, author packages, or do both.

```bash
curl -s http://127.0.0.1:8091/health
curl -s http://127.0.0.1:8091/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-11-25' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | python -c 'import json, sys; tools=json.load(sys.stdin)["result"]["tools"]; print(f"{len(tools)} tools"); print("\n".join("- " + tool["name"] for tool in tools))'
```

The local `serve` and ASGI servers use the same stateless Streamable HTTP
handler at `/mcp`. When `SEMANTIC_RAILS_API_KEYS` or `SEMANTIC_RAILS_API_KEY_FILE`
is configured, `/mcp` requires `Authorization: Bearer ...`, `X-API-Key`, or
`X-Semantic-API-Key`. Without configured keys, authentication is disabled for
local use. `/health` stays public. The endpoint validates Origin and MCP headers,
refuses bodies larger than 64 KiB, and resolves policy context from trusted
transport headers rather than tool arguments. Install a trusted policy-context
resolver before exposing either server to callers with different permissions.

Pip-installed stdio configuration can be generated from the project directory
so the command and package path match the local machine:

```bash
python - <<'PY'
import json
import shutil
from pathlib import Path

print(json.dumps({
    "mcpServers": {
        "semantic-rails": {
            "command": shutil.which("semantic-rails") or "semantic-rails",
            "args": ["mcp", "stdio", "--path", str(Path("my_package").resolve())],
        }
    }
}, indent=2))
PY
```

Source-checkout stdio configuration:

```json
{
  "mcpServers": {
    "semantic-rails": {
      "command": "uv",
      "args": ["run", "semantic-rails", "mcp", "stdio", "--package", "jaffle_shop"]
    }
  }
}
```

Cursor/Codex-style hosts can use the same stdio command. Hosts that support
Streamable HTTP can use the ASGI `/mcp` endpoint. Local compatibility testing
can use `http://127.0.0.1:8091/mcp` after starting the legacy HTTP transport.

Local profile defaults (`~/.semantic_rails/profiles.yml`) are a human CLI
convenience. MCP host configs should still pass an explicit `--path` or
`--package` so the host is deterministic, and deployed services should use their
own config/vault rather than reading a user's home directory.

## Transports and Protocol Versions

Two transports serve the same tools. They share one JSON-RPC dispatcher
(`semantic_rails.mcp_server.handle_jsonrpc_message`), so they return identical results:

| Entry point | Serves | Why it is kept |
|---|---|---|
| `semantic-rails mcp stdio` | stdio | Local agents such as Claude Code and Claude Desktop. The default. |
| ASGI or `semantic-rails serve` `/mcp` (`semantic_rails.mcp_streamable_http`) | Stateless Streamable HTTP | Network clients. Authenticated with the same API keys as `/api/v1/*`. |

Each tool result carries its payload twice: as `structuredContent`, and as compact JSON in
`content[0].text` for hosts that forward only text. Resource reads return compact JSON text.

The dispatcher negotiates `2025-11-25` (the default), `2025-03-26` or `2024-11-05` in
`initialize`. It already matches several parts of the `2026-07-28` revision:

- the Streamable HTTP endpoint keeps no sessions and sends no `Mcp-Session-Id`;
- `tools/list` returns tools in a fixed order;
- the workflow is in the server `instructions`;
- tool schemas are plain JSON Schema.

### Planned: the `2026-07-28` revision

Supporting `2026-07-28` alongside `2025-11-25` means gating the following on the protocol version
each request declares, so older clients see no change:

1. **Stateless requests.** Read `io.modelcontextprotocol/protocolVersion` (and client
   capabilities) from each request's `_meta` instead of requiring `initialize`, and answer an
   unsupported version with `UnsupportedProtocolVersionError` (`-32022`). Identify the server in
   each result's `_meta` (`io.modelcontextprotocol/serverInfo`).
2. **`server/discover`**, which the revision requires: supported versions, capabilities and
   server identity.
3. **`resultType: "complete"`** on every result. The server never needs `"input_required"`
   because no tool asks the client for more input.
4. **Cache hints.** Add `ttlMs` and `cacheScope` to `tools/list`, `prompts/list`,
   `resources/list`, `resources/read` and `resources/templates/list`. Resources depend on the
   caller's grants, so they are `"private"` behind an authenticated transport.
5. **Headers and errors.**
   - Check the `Mcp-Method` and `Mcp-Name` request headers on Streamable HTTP POSTs.
   - Decide whether an unknown resource keeps its structured error payload or becomes JSON-RPC
     `-32602`.
   - Stop answering `ping` and `logging/setLevel` for `2026-07-28` requests.

### MCP Python SDK 2.x

The engine pins `mcp<2`. Before lifting the pin, qualify these known integration points on
the chosen SDK version:

- **The Architect MCP.** `semantic_rails.architect_mcp` imports `mcp.server.fastmcp` at import
  time. Its tests also read `call_tool` results as a `(content, structured)` pair and read
  `Tool.inputSchema`; SDK 2.x returns a `CallToolResult` and names the attribute `input_schema`.
- **`httpx`.** Two engine tests import `httpx`, which only SDK 1.x brought in. It needs its own
  entry in the `dev` dependency group.
- **The lock file.** `pyproject.toml` and a regenerated `uv.lock` must change together.
  Dependabot's `<3` update fails CI for this reason: `uv sync --locked` rejects a constraint
  change without a matching lock.

## Structured Error Envelopes

Every error surfaced through the MCP or HTTP transport is wrapped in a structured envelope:

```json
{
  "code": "OBJECT_NOT_FOUND",
  "message": "Unknown object 'measure.jaffle.order_kount'",
  "severity": "error",
  "stage": "mcp",
  "details": {
    "object_id": "measure.jaffle.order_kount",
    "closest_matches": ["measure.jaffle.order_count", "measure.jaffle.large_order_count"]
  },
  "recovery_hints": [
    {
      "kind": "inspect_or_discover",
      "message": "Resolve the object ID through discover or inspect before querying.",
      "closest_matches": ["measure.jaffle.order_count", "measure.jaffle.large_order_count"]
    }
  ]
}
```

Each issue carries `code` and `message`, plus at least one of `details`, `recovery_hints`, or `closest_matches`. Over MCP, empty optional fields are left out; recovery hints keep their own details so each hint is actionable on its own. Bare `KeyError` / `AttributeError` leaks are wrapped as `INTERNAL_ERROR` envelopes with a bug-tracker hint so the surface is always actionable.

At MCP verbosity `minimal`, `MIXED_GRAIN_INVALID` omits the relationship analysis
dump while retaining offending dimensions, compatible measures and dimensions,
time-axis recovery, and every recovery hint. `REWRITE_APPLIED` omits `details.analysis`
and `details.path`, retaining its code, message, and `details.rewrite_kind`.
Request `compact` or `full` for analysis details within the same response budget. An unsupported
expression kind names the received kind and its request path (for example,
`query.select[0].expression.left`); recovery hints include the request shape to send.

### Error Code Catalog

| Code | One-line description |
|------|----------------------|
| `AMBIGUOUS_ALIAS` | Alias resolves to multiple semantic objects; pick one from `details.candidates`. |
| `AMBIGUOUS_CHILD_SCOPE` | Plain filters on one child entity across a one-to-many hop don't say which child rows they mean: two or more positive ones (the same row or separate ones), or one negated one ("has a row that is not X" or "has no row that is X"). `details.clarification.options` holds both readings, each as the query's whole rewritten `where`; resend one. Offered only when both answer for this caller. |
| `PLAN_UNMATCHED_TERMS` | A grouping option also carries `group_by` and `order_by` to apply with its complete `where` for one unclear term. With several, in `best.query_ir` remove each chosen option's `replaces` IDs from `group_by` and their `order_by` entries, add its `id`, keep `group_by` sorted, then validate. When two terms could replace the same grouping, plan offers no options; ask the user. |
| `AMBIGUOUS_PATH` | Several routes between root entity and target can answer differently and the package records none (`details.reason: route_decision_required`). `details.clarification` asks which one the question means (`question`) and lists one option per route: its `meaning` in business words, its `relationship_path`, and its `decision` row. Ask the person, then resend with that `decision`, the whole option, or its id in `route_decisions`. An id must uniquely answer the current query's clarification; unknown or ambiguous ids return `INVALID_QUERY` with offered ids in `closest_matches`. The clarification appears once in the error's details. Route and policy guards apply to every shape. A query decision applies to this query only; record it with Architect `record_route_decision` (the package default; an option's `conflicts_with` names the package rows to change first). |
| `DUPLICATE_OUTPUT_ALIAS` | Two projected columns share an alias; rename one. |
| `UNSUPPORTED_AGGREGATION` | Aggregation kind is not legal for this measure's class. For a measure restriction, `details.aggregation` records the rejected value and the hint's `aggregation_received` and `allowed` mirror `details.aggregation` and `details.allowed`; the hint lists only those allowed values and offers omitting `aggregation` when `details.default_aggregation` belongs to `details.allowed`, naming that default. Parameter errors disclose the required parameter schema. |
| `INVALID_TEMPORAL_ROLE` | Unknown temporal role; pick one from `details.compatible_temporal_roles`. |
| `INCOMPATIBLE_TEMPORAL_ROLE` | Selected role is not compatible with the chosen measure/metric, or the measure has no time role at all (`details.compatible` is empty; declare one on the model or the measure). |
| `INVALID_TEMPORAL_BINDING` | Time block targets a clock incompatible with a conversion's anchor; filter on `details.anchor_temporal_role` or push the constraint into a conversion metric. |
| `FANOUT_UNSAFE` | Breakdown crosses a 1-to-many relationship without a pre-aggregation boundary, or joins into a `temporal_validity` window without a query `time`. |
| `ROLLUP_UNSAFE` | Roll-up combines non-additive primitives; declare the aggregation entity or supply sketch metadata. For an `additive: false` measure summed above its stored grain, group by or filter (=) each key dimension. Keys are named only on validate, compile and run errors. The message, `details.key_dimensions` and hint name those dimensions only when every key column is a dimension of the caller's view; a key a hidden dimension declares keeps the generic refusal. |
| `MEASURE_VALIDITY_BOUNDARY` | Query crosses a declared measure-validity window; split by sub-window. |
| `OUT_OF_SCOPE` | Request isn't a governed-data query; hand off to the recommended tool — the semantic layer compiles governed data queries only. |
| `CUMULATIVE_TIME_FILTER_UNSUPPORTED` | Measure's accumulation semantics forbid the requested time filter: a bounded `time.start`, or a `where` filter on a date or calendar dimension other than an upper bound (`details.where_path`). Filters on the window's own measure input and applied row policies on temporal columns refuse too (`details.filter_source`), with no patch to remove the authored filter or policy. |
| `WINDOWED_TIME_FILTER_UNSUPPORTED` | Time-windowed filter cannot be applied to this query shape. `details.lookback` carries the metric's window; `recovery_hints` carries a `widen_time_window` patch with a concrete `suggested_start` and a `drop_time_start` patch with `{remove: ["time.start"]}`. A `where` filter on a date or calendar dimension other than an upper bound refuses the same way: `details.where_path` names it and the patch removes it. Filters on the window's own measure input and applied row policies on temporal columns also refuse (`details.filter_source`), with no time-boundary recovery patch. |
| `MIXED_GRAIN_INVALID` | Query mixes incompatible grains; split or rewrite. Compatible measure and dimension replacements rank naming-token overlap (id suffix, name and label) before character similarity. Replacements answer a different question and are suggestions for the caller to judge. |
| `NO_VALID_VALUES_SOURCE` | No `valid_values` source declared for the requested dimension. |
| `REWRITE_NOT_SUPPORTED` | Required rewrite is not implemented; try a simpler shape. A non-default `time.calendar_id`, or a `grain` on a time bound to a non-default calendar, refuses with `details.reason: calendar_not_supported_yet` and `details.calendar_id`: authored fiscal calendars return in a later release. |
| `INVALID_EXPRESSION_AST` | Expression AST is malformed; check the position-specific shape. An invalid `where` operator lists query filter operators and the null-test form: `op: "IS NULL"` / `"IS NOT NULL"`, omitting `value`. |
| `OBJECT_NOT_FOUND` | Referenced `object_id` does not exist for the caller; see `details.closest_matches`. An object hidden from the caller gets exactly the response of one the package doesn't have, and is never suggested. An existing measure with the exact namespace and name of a missing metric (or the reverse) is the first suggestion; unrelated typos keep same-kind matching. When what the caller may see can't be resolved, no suggestion is made. |
| `INVALID_QUERY` | Query IR fails structural validation. |
| `INVALID_CONFIG` | Package config is malformed, or the removed MCP interface v1 was requested. |
| `INVALID_METRIC_FILTER` | `metric_filters[]` entry is malformed; check the shape. |
| `INVALID_SEGMENT` | Segment definition is invalid. |
| `MISSING_DEPENDENCY` | Required upstream object is missing. |
| `QUERY_EXECUTION_ERROR` | Warehouse refused or aborted execution. |
| `PATH_NOT_FOUND` | No valid join path between the requested objects; `details.reason: excluded_by_decision` means every route walks a pair the package's `graph.path_preferences` rows (`details.rows`) record differently. `details.reachable_targets` and suggested group-by dimensions share compilation's path traversal and route-selection rules, respecting relationship directions, hop limits, route ambiguity, and recorded path preferences, including inherited decisions. The lists are exact under these path rules, without caching rejected routes, and are route-eligible: fan-out and policy checks still apply. Unrelated route rows retain bounded reachability scans; inherited-route searches skip branches that cannot reach the target within the remaining hops. |
| `POLICY_DENIED` | Policy context blocks a referenced object or query cut. `policy_effects` state each policy as the caller may see it: one that lists a hidden object, or whose text names one, appears without `policy_id` and with its action's fixed text. While anything is hidden from the caller, a raw-column aggregate is refused with empty `blocked_objects`, `policy_effects` and `policy_violations`. When what the caller may see can't be resolved, every tool refuses before binding or the warehouse with `details: {"reason": "visibility_unresolved"}`, naming nothing. |
| `INVALID_METRIC_PREDICATE` | `metric_predicates[]` entry is malformed. |
| `PREDICATE_SCOPE_UNSAFE` | Predicate scope is incompatible with query grain. |
| `PREDICATE_CONTEXT_ENTITY_INCOMPATIBLE` | Predicate context entity disagrees with the surrounding query. |
| `PREDICATE_FILTER_INCOMPATIBLE` | Predicate filter cannot be expressed for the chosen entity. |
| `PREDICATE_GRAIN_UNSAFE` | Predicate would silently change query grain. |
| `PREDICATE_ENTITY_REQUIRED` | Predicate must declare an entity. |
| `PREDICATE_INPUT_REQUIRED` | Predicate is missing a required input. |
| `PREDICATE_NOT_SUPPORTED` | Predicate kind is not supported for this measure. |
| `INVALID_ORDER_BY` | `order_by[]` entry has the wrong shape. |
| `CONVERSION_NOT_SUPPORTED` | Conversion semantics are not supported for this metric. |
| `CONVERSION_ENTITY_REQUIRED` | Conversion must declare an anchor entity. |
| `CONVERSION_WINDOW_REQUIRED` | Conversion needs `window: {unit, value}` with a supported unit (`minute` … `year`) and a positive value. |
| `CONVERSION_MATCHING_MODE_REQUIRED` | Conversion needs `matching_mode`: `first_converted_after_base` or `closest_converted_after_base`. `details.allowed_values` says what each matches and `details.expression` is the sent expression with the first one set. A conversion metric's `inspect` card shows its own expression under `conversion`, to run it over another window. |
| `UNKNOWN_MCP_PROMPT` | Prompt name isn't in the catalog; see `details.available_prompts`. |
| `UNKNOWN_MCP_RESOURCE` | Resource URI isn't in the catalog; see `details.available_resources`. |
| `UNKNOWN_MCP_TOOL` | Tool name isn't in `tools/list`; see `details.available_tools`. |
| `INVALID_MCP_ARGUMENTS` | Tool arguments don't match the input_schema; `recovery_hints` carries the corrected shape. |
| `RESULT_TOO_LARGE` | Required tool response fields exceed the shared character budget after optional detail is trimmed. No partial answer is returned; request fewer rows, columns or objects. See `details.max_result_chars`. |
| `WINDOW_TOTAL_UNSUPPORTED` | A `time` window with no `grain` would return one total, but part of the query still groups by the raw time column, so the result can't be one row per group. Nothing is returned. Set `time.grain`, or remove `time.start` and `time.end`. |
| `EMPTY_GROUPS_UNSETTLED` | The compiler built a query that reads a sum or count without settling its empty groups, so a group with no rows would read `NULL` instead of `0`, or a sum missing a required row count, so a group whose amounts are all unknown could read `0`. A measure containing a nested CASE forced onto a rollup is also refused (`details.aggregate_relation`). An engine defect, not a query error; nothing is returned. `details.measures` names them, or `details.row_counts_named_like` a row count named like another column. |
| `INTERNAL_ERROR` | Bare exception reached the boundary; retry once and file a bug if it recurs. |

### Worked Example Envelopes

`OBJECT_NOT_FOUND` on `inspect` with a typo:

```json
{
  "code": "OBJECT_NOT_FOUND",
  "message": "Unknown object 'measure.jaffle.order_kount'",
  "details": {"object_id": "measure.jaffle.order_kount",
              "closest_matches": ["measure.jaffle.order_count"]},
  "recovery_hints": [{"kind": "inspect_or_discover",
                       "closest_matches": ["measure.jaffle.order_count"]}]
}
```

`INVALID_MCP_ARGUMENTS` when `query` is passed as a string:

```json
{
  "code": "INVALID_MCP_ARGUMENTS",
  "message": "Argument 'query' must be a JSON object.",
  "details": {"field": "query", "argument_type": "str"},
  "recovery_hints": [{"kind": "wrap_query_as_object",
                       "closest_valid_query": {"version": 1, "select": []}}]
}
```

`INTERNAL_ERROR` when a bare exception escapes the handler. The message is fixed engine text
over MCP, JSON-RPC and HTTP alike; the exception's own text goes to the server log only:

```json
{
  "code": "INTERNAL_ERROR",
  "message": "An unexpected engine error occurred; the detail is in the server log.",
  "details": {"exception_type": "KeyError"},
  "recovery_hints": [{"kind": "file_bug_report",
                       "message": "...file a bug at .../issues..."}]
}
```

## Measuring Context Cost

`scripts/mcp_context.py` measures how much context the query MCP costs an agent, and how often
`plan` drafts the right query. It drives the packaged server in process, through the same JSON-RPC
dispatcher as `semantic-rails mcp stdio`, against a throwaway copy of `jaffle_shop` whose DuckDB file
it builds from the seed data. Token counts are `round(chars / 4)` of the JSON a model sees: the
compact `structuredContent` (what Claude Code forwards) and the `content[0].text` channel (what
text-forwarding hosts forward). The proxy needs no tokenizer. Compare runs with each other, not with
provider bills.

```bash
uv run python scripts/mcp_context.py                  # report, then fail on any gate
uv run python scripts/mcp_context.py --markdown       # tables for a PR description
uv run python scripts/mcp_context.py --write-baseline # after an intended change
uv run python scripts/mcp_context.py --eval-file PATH # score a copy of a frozen split
```

`tests/semantic_rails/test_mcp_context.py` runs the same gates in CI. Their data lives in
`tests/semantic_rails/mcp_context/`:

| Gate | Fails when |
|---|---|
| Context budgets (`budgets.json`) | A measured size exceeds its budget by more than 2% (and at least 8 tokens), a count such as the number of tools exceeds its budget at all, a size has no budget, or a budgeted size is no longer measured. |
| Planner accuracy (`plan_accuracy_baseline.json`) | A gold case's `plan(detail="query")` outcome gets worse, or a wrong case gets a slot wrong that it used to get right. |
| Gold answers | A gold query fails, its rows no longer match the frozen answer, or a listed alternative answers differently. |
| Frozen eval set | `eval_jaffle.jsonl` no longer matches `DEV_SET_SHA256` in the script. |

The budgets cover `tools/list` and the `initialize` instructions (`query.v2.tools_list.*`,
`query.v2.instructions_tokens`), the resource and prompt lists and every resource read
(`query.resource*`, `query.prompts_list_tokens`), one call per tool and mode at its defaults,
including a time window without a grain, and the largest of them (`query.v2.default.*`), two
metadata calls behind an authenticated transport (`query.hosted.*`), four common mistakes
(`query.error.*`), and two scripted three-question sessions (`query.v2.session.*`). Three of the
mistakes fail with a specific error code; the fourth,
a misspelled `discover` argument, succeeds with a warning. A scripted call that fails when it should
succeed (or the reverse), or that reports a different code, stops the measurement rather than
counting as a smaller response. A scripted session's queries may only use ids that an earlier call
in the same session returned, so its size measures a path an agent could follow. Architect MCP
tool-list sizes are recorded under `tracked` and are not gated.

A draft is correct when it matches the gold query, or a listed alternative, in every slot that can
change its rows: measures and metrics, grouping, time role, grain, window, `fill`, calendar,
filters, metric filters, temporal role overrides, path policy and limit, plus the sort for
rankings. Its rows must also match the frozen answer. An unanswerable question is answered
correctly when `plan` refuses it as `out_of_scope` or `unrealizable`. Each outcome is one of:

- `pass`: correct, and the response reports `ok` with no warnings.
- `pass_flagged`: correct, but the response still reports a non-`ok` status or a warning: a false
  alarm, which costs the agent a needless repair.
- `wrong_flagged`: wrong, and the status isn't `ok`.
- `wrong_warned`: wrong, with status `ok` but a warning that signals doubt.
- `wrong_silent`: wrong, and the response reports `ok` with no warnings.

A `plan` call that fails outright stops the run instead of being graded. The baseline records
each case's outcome and the slots it gets wrong (`answer` when every slot matches but the rows
don't), so a case that is already wrong can't quietly get worse.

When a change is intended, such as a smaller response or a planner fix, run `--write-baseline` and
commit the updated budgets or outcomes with it. The report lists sizes under budget and cases that
improved, so savings get locked in. A budget moves only when its size moves beyond the tolerance,
and `--write-baseline` refuses to run while a gold answer fails or the eval set has changed.

`scripts/agent_harness/eval_ab.py` runs the same eval questions through a real agent instead: the
agent harness has a model behind any OpenAI-compatible chat endpoint answer each question once per
build of the query MCP (`--arms name=command`), and each run's check scores the query the model
last ran against the gold rows. It reports correct and silent wrong answers, tokens and tool calls
per arm (see `scripts/agent_harness/README.md`). Its numbers depend on the model, so compare arms
within one set of runs.

The release workflows also run `scripts/benchmark_plan.py --gate` over the blind-agent corpus.
That gate checks that plans are actionable, carry the expected IDs and Query IR fields, stay
smaller than `detail="full"`, and hit the compile cache. This one checks drafted queries against
gold answers and tells flagged mistakes from silent ones.

### Eval Set

`eval_jaffle.jsonl` is the frozen development split: 38 questions covering trends, calendar
windows, rankings, multi-value filters, near-duplicate metrics, ratios, time expressions, a segment
metric, out-of-scope questions and misspellings. Each answerable case has a hand-written gold query,
any equivalent alternatives, and its frozen answer. Answers compare as sets of rows whose columns
are named by what they hold (a measure or metric, a dimension, or the time bucket), so aliases and
column order don't matter but a value in the wrong column does. A dimension pinned to one value by
a filter is left out, as it adds only a constant column. Numbers match within a relative tolerance
of 1e-8, row order counts only for rankings, and time buckets count only for trends.

A held-out split of 12 more questions is kept outside the repository so the planner can't be tuned
against it. `HELDOUT_SET_SHA256` in the script commits to its content. `--eval-file` scores a file
only when it matches the dev or held-out digest, unless `--allow-unfrozen` is passed.

## Production Readiness

The packaged MCP server is suitable for local agents and trusted service wrappers that need stable
tool/resource/prompt definitions, structured JSON-RPC errors, request IDs, and optional API-key
protection. Process supervision, network TLS, host-level authorization, tenant isolation, and
secret rotation remain deployment responsibilities rather than MCP protocol logic.
